# tests/python/forensics/test_vol3_support.py
"""Unit tests for Volatility 3 support — feature flag and enqueue guards."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import JobRepository
from astinuslab.forensics.vol3_tools import ToolDependencyError
from tests.python._support.helpers import autospec_setattr


@pytest.fixture(autouse=True)
def _ready_local_dwarf2json(monkeypatch):
    import astinuslab.forensics.vol3_support as v3

    def require_ready(settings_obj):
        if not hasattr(settings_obj, "vol3_dwarf2json_sha256"):
            settings_obj.vol3_dwarf2json_sha256 = ""
        return Path(getattr(settings_obj, "vol3_dwarf2json_path", None) or "/verified/dwarf2json")

    monkeypatch.setattr(
        v3,
        "require_dwarf2json_tool",
        require_ready,
        raising=False,
    )


def _create_completed_job_with_dump(scope):
    """Create a job and force it to completed status with a dump path."""
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        # Force to completed via force_transition (bypasses state machine for tests)
        repo.force_transition(
            job.id,
            "completed",
            dump_path="/storage/dumps/job-x/memory.dump",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        return job.id


class FakeSettings:
    vol3_enabled = True


class FakeSettingsDisabled:
    vol3_enabled = False


def test_isf_publication_and_lock_are_shared_under_service_umask(tmp_path, monkeypatch):
    import astinuslab.forensics.vol3_support as v3

    symbols = tmp_path / "symbols"
    monkeypatch.setattr(v3, "settings", SimpleNamespace(vol3_symbols_path=symbols))
    previous_umask = os.umask(0o007)
    try:
        final = symbols / "linux/kernel.json"
        with v3._isf_cache_lock("kernel"):
            temporary = v3._new_temp_isf_path(final)
            temporary.write_text('{"metadata":{"linux":{}},"symbols":{"init_task":{}}}')
            assert v3._replace_with_valid_isf(temporary, final) == (True, None)
        assert final.stat().st_mode & 0o777 == 0o660
        assert (final.parent / ".lock-kernel").stat().st_mode & 0o777 == 0o660
        assert final.parent.stat().st_mode & 0o7777 == 0o2770
        assert symbols.stat().st_mode & 0o7777 == 0o2770
        assert final.stat().st_gid == symbols.stat().st_gid
    finally:
        os.umask(previous_umask)


def test_invalid_isf_replacement_preserves_published_symbol(tmp_path):
    import astinuslab.forensics.vol3_support as v3

    final = tmp_path / "kernel.json"
    original = '{"metadata":{"linux":{}},"symbols":{"init_task":{}}}'
    final.write_text(original)
    temporary = v3._new_temp_isf_path(final)
    temporary.write_text("invalid")
    valid, _reason = v3._replace_with_valid_isf(temporary, final)
    assert valid is False
    assert final.read_text() == original
    assert not temporary.exists()


class TestIsVol3Enabled:
    def test_returns_true_by_default(self, monkeypatch):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "settings", FakeSettings())
        assert v3.is_vol3_enabled() is True

    def test_returns_false_when_disabled(self, monkeypatch):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "settings", FakeSettingsDisabled())
        assert v3.is_vol3_enabled() is False


class TestRunVol3SymbolBuild:
    def _make_live_job(
        self,
        in_memory_session_scope,
        *,
        target_id="ubuntu-22-04-default",
        kernel_release="6.8.0-101-generic",
        distro="ubuntu",
        version="22.04",
    ):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id=target_id,
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "running",
                detected_kernel_release=kernel_release,
                detected_distro=distro,
                detected_version=version,
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            return job.id

    def test_returns_false_when_no_target_runtime(self, in_memory_session_scope, monkeypatch):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        # No runtime created → ip_address will be None
        with in_memory_session_scope() as session:
            from astinuslab.api.schemas import JobCreate
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            job_id = job.id
        result = v3.run_vol3_symbol_build(job_id)
        assert result is False

    def test_returns_false_when_no_ssh_key(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (_ for _ in ()).throw(RuntimeError("no key")))
        with in_memory_session_scope() as session:
            from astinuslab.api.schemas import JobCreate
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            job_id = job.id
        result = v3.run_vol3_symbol_build(job_id)
        assert result is False

    def test_returns_false_on_subprocess_error(self, in_memory_session_scope, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        with in_memory_session_scope() as session:
            from astinuslab.api.schemas import JobCreate
            from astinuslab.db.repositories import JobRepository

            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            job_id = job.id

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = v3.run_vol3_symbol_build(job_id)
        assert result is False

    def test_timeout_maps_to_false_result(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["build"], timeout=300)
            result = v3.run_vol3_symbol_build(job_id)

        assert result is False
        assert mock_run.call_args.kwargs["timeout"] == 300
        assert (tmp_path / "storage" / "dumps" / job_id / "vol3" / "symbol-build-host.log").exists()

    def test_returns_true_on_success(self, in_memory_session_scope, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        job_id = self._make_live_job(in_memory_session_scope)

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "success-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)
        assert result is True

    def test_requires_fresh_dwarf2json_and_exports_exact_live_guest_env(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        expected_sha256 = "ab" * 32
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_sha256=expected_sha256,
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake", encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda *_args: None)
        job_id = self._make_live_job(in_memory_session_scope)
        resolved = tmp_path / "operator tools" / "verified dwarf2json"
        events: list[tuple[str, object]] = []

        def require(settings_obj):
            events.append(("require", settings_obj))
            return resolved

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            events.append(("run", dict(env or {})))
            isf_path = tmp_path / "success-live.json"
            isf_path.write_text(
                '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}',
                encoding="utf-8",
            )
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        monkeypatch.setattr(v3, "require_dwarf2json_tool", require)
        monkeypatch.setattr(v3.subprocess, "run", fake_run)

        result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert [event[0] for event in events] == ["require", "run"]
        run_env = events[1][1]
        assert isinstance(run_env, dict)
        assert run_env["DWARF2JSON_LOCAL"] == str(resolved)
        assert run_env["DWARF2JSON_SHA256"] == expected_sha256

    def test_invasive_install_suppressed_for_capture_job(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_allow_invasive_guest_symbol_install=True,
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)
        captured_env: dict[str, str] = {}

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            captured_env.update(env or {})
            isf_path = tmp_path / "success-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert captured_env["VOL3_ALLOW_GUEST_PACKAGE_INSTALL"] == "0"
        with in_memory_session_scope() as session:
            event_states = [event.state for event in JobRepository(session).list_events(job_id)]
        assert "vol3_invasive_install_suppressed" in event_states

    def test_invasive_install_allowed_for_isf_only_job(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_allow_invasive_guest_symbol_install=True,
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            JobRepository(session).update_job(job_id, capture_type="none")
        captured_env: dict[str, str] = {}

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            captured_env.update(env or {})
            isf_path = tmp_path / "success-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert captured_env["VOL3_ALLOW_GUEST_PACKAGE_INSTALL"] == "1"
        with in_memory_session_scope() as session:
            event_states = [event.state for event in JobRepository(session).list_events(job_id)]
        assert "vol3_invasive_install_enabled" in event_states

    def test_invasive_install_disabled_setting_stays_off_for_isf_only_job(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            JobRepository(session).update_job(job_id, capture_type="none")
        captured_env: dict[str, str] = {}

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            captured_env.update(env or {})
            isf_path = tmp_path / "success-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert captured_env["VOL3_ALLOW_GUEST_PACKAGE_INSTALL"] == "0"
        with in_memory_session_scope() as session:
            event_states = [event.state for event in JobRepository(session).list_events(job_id)]
        assert not any(state.startswith("vol3_invasive_install_") for state in event_states)

    def test_invalid_cached_live_symbol_is_not_reused(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        job_id = self._make_live_job(in_memory_session_scope)
        cached_isf = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_bytes(b"")

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "rebuilt-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run) as mock_run:
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert mock_run.called
        assert (
            cached_isf.read_text(encoding="utf-8")
            == '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'
        )

    def test_passes_target_default_username_to_live_symbol_script(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch,
            v3,
            "target_by_id",
            lambda target_id: {
                "id": target_id,
                "default_username": "debian",
            },
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        job_id = self._make_live_job(in_memory_session_scope, target_id="debian-12-default")

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "username-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run) as mock_run:
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        cmd = mock_run.call_args.args[0]
        assert "--guest-user" in cmd
        assert cmd[cmd.index("--guest-user") + 1] == "debian"

    def test_valid_live_isf_creates_live_guest_metadata(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            symbols_dir = cmd[cmd.index("--symbols-dir") + 1]  # noqa: F841
            isf_path = tmp_path / "staged-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        isf_path = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        assert isf_path.exists()
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "live_guest"
            assert artifact.path == str(isf_path)
            assert artifact.sha256 is not None

    def test_unsupported_distro_rc2_records_distinct_event(self, in_memory_session_scope, monkeypatch, tmp_path):
        """If build_vol3_symbol.sh exits 2 (zypper/pacman/apk branch),
        the job event log must record a distinguishable 'vol3_unsupported_distro' event
        so the user knows it is a distro-coverage gap, not a generic build failure."""
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "opensuse"}
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        job_id = self._make_live_job(
            in_memory_session_scope,
            target_id="opensuse-leap-15-default",
            distro="opensuse",
            version="15",
            kernel_release="6.4.0-150500",
        )

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=2)
            result = v3.run_vol3_symbol_build(job_id)

        assert result is False

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            events = repo.list_events(job_id)
            states = [e.state for e in events]
            msg_match = next((e.message for e in events if e.state == "vol3_unsupported_distro"), None)
        assert "vol3_unsupported_distro" in states, states
        assert msg_match is not None
        assert "not yet supported" in msg_match.lower(), msg_match

    def test_invalid_live_isf_is_rejected_by_validation(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        job_id = self._make_live_job(in_memory_session_scope)

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "invalid-live.json"
            isf_path.write_text("{}", encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is False
        assert not (tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json").exists()
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            assert repo.get_latest_isf_artifact(job_id) is None


class TestRunVol3IsfOnly:
    def _make_job(self, in_memory_session_scope, distro="ubuntu", version="22.04", kernel_release="6.8.0-101-generic"):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_distro=distro,
                detected_version=version,
                detected_kernel_release=kernel_release,
            )
            return job.id

    def test_returns_failure_when_fetch_returns_none(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (None, None))

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False
        assert "ISF build failed" in summary

    @pytest.mark.parametrize(
        ("fetched_vmlinux", "expected_lead", "rejected_lead"),
        [
            (False, "no vmlinux obtained", "no ISF built from the fetched vmlinux"),
            (True, "no ISF built from the fetched vmlinux", "no vmlinux obtained"),
        ],
        ids=("nothing-fetched", "vmlinux-fetched"),
    )
    def test_failure_summary_lead_names_the_half_of_the_pipeline_that_failed(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
        fetched_vmlinux,
        expected_lead,
        rejected_lead,
    ):
        """The "no vmlinux obtained" clause is false when a provider did fetch one.

        debuginfod and host_fetch return a vmlinux that dwarf2json then has to
        convert; when only the conversion failed, blaming the download sends the
        operator to the wrong place.  The appended reason disambiguates it, but the
        leading clause is what an operator reads first.
        """
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import IsfBuildOutcome, KernelMetadata, ProviderAttempt
        from astinuslab.forensics.symbols.identity import SymbolIdentity

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        outcome = IsfBuildOutcome(
            success=False,
            isf_path=None,
            identity=SymbolIdentity(
                architecture="x86_64",
                kernel_release="6.8.0-101-generic",
                build_id=None,
                source="host_fetch",
            ),
            metadata=KernelMetadata(distro="ubuntu", version="22.04", kernel_release="6.8.0-101-generic"),
            attempts=[
                ProviderAttempt(
                    name="host_fetch",
                    error="dwarf2json is not ready",
                    vmlinux_path=str(tmp_path / "vmlinux") if fetched_vmlinux else None,
                )
            ],
        )
        autospec_setattr(monkeypatch, v3, "_run_symbol_provider_chain", lambda *_args, **_kwargs: outcome)
        autospec_setattr(monkeypatch, v3, "run_vol3_symbol_build", lambda job_id, **_kwargs: False)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False
        assert f"ISF build failed: {expected_lead} for distro=" in summary
        assert rejected_lead not in summary
        assert "reason: dwarf2json is not ready" in summary

    def test_fetch_failure_persists_job_local_host_log(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        def fake_fetch(log_path, *_args, **_kwargs):
            log_path.write_text("download interrupted while fetching dbgsym\n", encoding="utf-8")
            return None, None

        monkeypatch.setattr(v3, "_run_fetch_vmlinux", fake_fetch)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        log_path = tmp_path / "storage" / "dumps" / job_id / "vol3" / "symbol-build-host.log"
        assert success is False
        assert "ISF build failed" in summary
        assert log_path.exists()
        assert "download interrupted" in log_path.read_text(encoding="utf-8")
        assert str(log_path) in summary

    def test_host_fetch_failure_can_succeed_with_live_guest_fallback(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (None, None))
        fallback_calls = []

        def fake_symbol_build(job_id, **kwargs):
            fallback_calls.append((job_id, kwargs))
            return True

        monkeypatch.setattr(v3, "run_vol3_symbol_build", fake_symbol_build)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        assert "live guest fallback" in summary.lower()
        assert fallback_calls == [(job_id, {"log_name": "symbol-build-live.log"})]

    def test_returns_failure_when_job_not_found(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        success, summary = v3.run_vol3_isf_only("nonexistent-job-id")

        assert success is False
        assert "not found" in summary

    def test_returns_success_when_symbol_already_exists(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        cached_isf = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "_symbol_path", lambda kernel_release, build_id=None: cached_isf)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        assert "skipped" in summary.lower() or "already exists" in summary.lower()

    def test_returns_failure_when_dwarf2json_fails(self, in_memory_session_scope, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))

        job_id = self._make_job(in_memory_session_scope)

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False

    def test_creates_linux_symbols_dir_before_writing_isf(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text(
            '#!/usr/bin/env bash\nprintf \'{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}\'\n',
            encoding="utf-8",
        )
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_distro="ubuntu",
                detected_version="22.04",
                detected_kernel_release="6.8.0-101-generic",
            )
            job_id = job.id

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        assert (symbols_dir / "linux" / "6.8.0-101-generic.json").exists()

    def test_returns_failure_when_isf_validation_fails(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        # dwarf2json writes {} (empty dict) to stdout → rc=0 but invalid ISF
        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text("#!/usr/bin/env bash\nprintf '{}'\n", encoding="utf-8")
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False
        assert "validation" in summary.lower() or "empty" in summary.lower() or "not a json" in summary.lower()
        isf_path = symbols_dir / "linux" / "6.8.0-101-generic.json"
        assert not isf_path.exists(), "Partial ISF file should have been cleaned up"

    def test_records_host_fetch_artifact_on_success(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text(
            '#!/usr/bin/env bash\nprintf \'{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}\'\n',
            encoding="utf-8",
        )
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "host_fetch_unverified"
            assert artifact.sha256 is not None

    def test_records_cache_artifact_for_existing_symbol(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        cached_isf = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "_symbol_path", lambda kernel_release, build_id=None: cached_isf)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache_soft_hit"
            assert artifact.sha256 is not None

    def test_invalid_cached_symbol_is_rebuilt_not_recorded_as_cache(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text("not json", encoding="utf-8")

        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text(
            '#!/usr/bin/env bash\nprintf \'{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}\'\n',
            encoding="utf-8",
        )
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        assert (
            cached_isf.read_text(encoding="utf-8")
            == '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'
        )
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "host_fetch_unverified"

    def test_unsupported_distro_event_dedups_generic_failure_summary(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        """When live-guest exits rc=2 (vol3_unsupported_distro event already
        emitted), run_vol3_isf_only's summary surfaces that event's message
        instead of layering a generic 'host fetch failed and live-guest fallback
        also failed' line on top."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *a, **kw: (None, None))

        def fake_live_guest(job_id, **kwargs):
            # Simulate run_vol3_symbol_build's rc=2 path by adding the event,
            # then return False.
            with in_memory_session_scope() as session:
                from astinuslab.db.repositories import JobRepository

                repo = JobRepository(session)
                repo.add_event(
                    job_id,
                    "vol3_unsupported_distro",
                    "Vol3 live-guest build is not yet supported on this distro; "
                    "install kernel debug symbols manually inside the guest.",
                )
            return False

        monkeypatch.setattr(v3, "run_vol3_symbol_build", fake_live_guest)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False
        assert "not yet supported" in summary, summary
        assert "host fetch failed" not in summary, summary
        assert "live-guest fallback also failed" not in summary, summary

    def test_generic_failure_path_unchanged_when_no_unsupported_event(
        self,
        in_memory_session_scope,
        monkeypatch,
        tmp_path,
    ):
        """When live-guest fails for a generic reason (no event, or a different
        event state), the existing host-msg + log tail is still surfaced."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *a, **kw: (None, None))
        autospec_setattr(monkeypatch, v3, "run_vol3_symbol_build", lambda job_id, **kw: False)

        job_id = self._make_job(in_memory_session_scope)
        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is False
        assert "ISF build failed" in summary


class TestBuildIdPersistence:
    """F02: ISF cache disambiguation by build_id.

    The build_id column on ISFArtifact is the data foundation for detecting
    kernel rebuilds with the same uname -r but different DWARF. These tests
    verify the column is populated on both the host_fetch and live_guest
    paths.
    """

    def test_extract_build_id_returns_none_when_readelf_missing(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        def boom(*a, **k):
            raise FileNotFoundError("readelf")

        monkeypatch.setattr(v3.subprocess, "run", boom)
        result = v3._extract_build_id(tmp_path / "vmlinux")
        assert result is None

    def test_extract_build_id_parses_readelf_output(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        import astinuslab.forensics.vol3_support as v3

        readelf_stdout = (
            "Displaying notes found in: .notes\n"
            "  Owner                Data size  Description\n"
            "  GNU                  0x00000014 NT_GNU_BUILD_ID (unique build ID bitstring)\n"
            "    Build ID: 4f8b3a20e4a3c1d2e7b5\n"
        )
        autospec_setattr(
            monkeypatch,
            v3.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=0, stdout=readelf_stdout),
        )
        result = v3._extract_build_id(tmp_path / "vmlinux")
        assert result == "4f8b3a20e4a3c1d2e7b5"

    def test_extract_build_id_rejects_non_hex(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        import astinuslab.forensics.vol3_support as v3

        autospec_setattr(
            monkeypatch,
            v3.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=0, stdout="    Build ID: not-a-hex-string\n"),
        )
        assert v3._extract_build_id(tmp_path / "vmlinux") is None

    def test_host_fetch_persists_build_id_on_artifact(self, in_memory_session_scope, monkeypatch, tmp_path):
        """A successful host_fetch path stores the extracted build_id on ISFArtifact."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text(
            '#!/usr/bin/env bash\nprintf \'{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}\'\n',
            encoding="utf-8",
        )
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda path: "deadbeefcafe1234")

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_distro="ubuntu",
                detected_version="22.04",
                detected_kernel_release="6.8.0-101-generic",
            )
            job_id = job.id

        success, _ = v3.run_vol3_isf_only(job_id)
        assert success is True

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "host_fetch_unverified"
            assert artifact.build_id == "deadbeefcafe1234"

    def test_live_guest_persists_build_id_from_script_output(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_symbol_build extracts build_id from the keyed script output and stores it."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "running",
                detected_kernel_release="6.8.0-101-generic",
                detected_distro="ubuntu",
                detected_version="22.04",
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            job_id = job.id

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "rebuilt-live.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            stdout.write(b"build_id=cafef00d1234567890\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "live_guest"
            assert artifact.build_id == "cafef00d1234567890"

    def test_live_guest_tolerates_missing_build_id_in_script_output(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        """If the guest script does not emit build_id (older guest image, no readelf),
        the artifact is still created — build_id is just NULL."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(
            monkeypatch, v3, "target_by_id", lambda target_id: {"id": target_id, "default_username": "ubuntu"}
        )
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "running",
                detected_kernel_release="6.8.0-101-generic",
                detected_distro="ubuntu",
                detected_version="22.04",
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            job_id = job.id

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            isf_path = tmp_path / "no-buildid.json"
            isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
            stdout.write(f"isf_path={isf_path}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.build_id is None


class TestBuildIdCacheEnforcement:
    """F02: lookup-side build_id enforcement for both live-guest and host-fetch paths.

    Verifies that:
    - A cached ISF with a *different* build_id than the current kernel causes a cache
      miss so the ISF is rebuilt for the correct kernel build.
    - A cached ISF whose stored build_id is NULL (old artifact) is a soft-hit: the
      cache is accepted and the discovered build_id is recorded on the new artifact.
    - A cached ISF whose stored build_id *matches* the current kernel is a true hit.
    - Probe failure (readelf absent, SSH hiccup) degrades to a soft-hit.
    """

    _VALID_ISF = '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'

    # ------------------------------------------------------------------
    # run_vol3_isf_only (host-fetch path)
    # ------------------------------------------------------------------

    def _make_isf_only_job(self, in_memory_session_scope, kernel_release="6.8.0-101-generic"):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_kernel_release=kernel_release,
                detected_distro="ubuntu",
                detected_version="22.04",
            )
            return job.id

    def test_isf_only_build_id_mismatch_is_cache_miss(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_isf_only: mismatch between job build_id and cached ISF build_id → rebuild."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        symbols_dir = tmp_path / "symbols"
        fake_dwarf2json = tmp_path / "dwarf2json"
        fake_dwarf2json.write_text(f"#!/usr/bin/env bash\nprintf '{self._VALID_ISF}'\n", encoding="utf-8")
        fake_dwarf2json.chmod(0o755)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=fake_dwarf2json,
                storage_dir=tmp_path / "storage",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *a, **k: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda _p: "newbuildid1234")

        # Place a valid ISF file in the symbols cache.
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_isf_only_job(in_memory_session_scope)

        # Record a *prior* live-guest artifact for this job with the CURRENT kernel's
        # build_id ("newbuildid1234") — this is what get_job_known_build_id returns.
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id="newbuildid1234",
            )
        # The cross-job "cached" artifact has a DIFFERENT (stale) build_id.
        # Because it is created AFTER the job artifact, find_valid_isf_by_kernel
        # returns this row — giving cached_build_id="oldbuildidaaaa" ≠ job's "newbuildid1234".
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id="oldbuildidaaaa",
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        # A rebuild happened — the new artifact source must be host_fetch, not cache.
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "host_fetch", f"Expected host_fetch (rebuild), got {artifact.source!r}"

    def test_isf_only_null_cached_build_id_is_soft_hit(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_isf_only: cached ISF has NULL build_id → soft-hit; job build_id recorded."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        # Place a valid ISF in the cache.
        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_isf_only_job(in_memory_session_scope)

        # Record a prior live-guest artifact for this job — this provides the
        # job's known build_id.  The cross-job cached ISF has no build_id (NULL).
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id="feedface99",
            )
        # The "global cached" artifact for this kernel has NULL build_id.
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(cached_isf),
                "host_fetch",
                # no build_id — simulates an old artifact
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache_soft_hit"
            # Soft-hit should record the job's known build_id on the new artifact.
            assert artifact.build_id == "feedface99"

    def test_isf_only_soft_hit_records_cache_soft_hit_source(self, in_memory_session_scope, monkeypatch, tmp_path):
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        cached_isf = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")
        job_id = self._make_isf_only_job(in_memory_session_scope)

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache_soft_hit"

    def test_isf_only_corroborated_legacy_hit_records_cache_source(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )
        cached_isf = tmp_path / "symbols" / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")
        job_id = self._make_isf_only_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(job_id, "6.8.0-101-generic", str(cached_isf), "live_guest", build_id="feedface99")
            other_job = repo.create_job(
                JobCreate(target_id="ubuntu-22-04-default", scenario_id="immediate-capture", execution_mode="immediate")
            )
            repo.create_isf_artifact(
                other_job.id, "6.8.0-101-generic", str(cached_isf), "host_fetch", build_id="feedface99"
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache"

    def test_validate_isf_caches_by_stat(self, tmp_path, monkeypatch):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "isf.json"
        f.write_text('{"metadata":{"linux":{}},"symbols":{"init_task":{}}}', encoding="utf-8")
        v3._ISF_VALIDATE_CACHE.clear()
        assert v3._validate_isf(f) == (True, None)

        calls = []
        real_loads = v3.json.loads

        def tracked_loads(data):
            calls.append(1)
            return real_loads(data)

        monkeypatch.setattr(v3.json, "loads", tracked_loads)
        assert v3._validate_isf(f) == (True, None)
        assert calls == []

        f.write_text('{"metadata":{"linux":{}},"symbols":{"x":{},"y":{}}}', encoding="utf-8")
        assert v3._validate_isf(f) == (True, None)
        assert calls == [1]

    def test_isf_only_matching_build_id_is_cache_hit(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_isf_only: matching build_ids → true cache hit, no rebuild."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        symbols_dir = tmp_path / "symbols"
        shared_build_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic_aaaaaaaaaaaaaaaa.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_isf_only_job(in_memory_session_scope)

        # Job's prior live-guest artifact has the same build_id as the cached ISF.
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id=shared_build_id,
            )
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(cached_isf),
                "host_fetch",
                build_id=shared_build_id,
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache"
            assert artifact.build_id == shared_build_id

    def test_isf_only_uses_build_id_qualified_cache_hit(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_isf_only: a known build_id finds a qualified-only cache file."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        build_id = "1234567890abcdef"
        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / f"6.8.0-101-generic_{build_id}.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")
        stale_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        stale_isf.write_text(self._VALID_ISF, encoding="utf-8")

        def fail_fetch(*args, **kwargs):  # pragma: no cover
            raise AssertionError("qualified cache hit should not fetch vmlinux")

        monkeypatch.setattr(v3, "_run_fetch_vmlinux", fail_fetch)

        job_id = self._make_isf_only_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id=build_id,
            )
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(stale_isf),
                "host_fetch",
                build_id="aaaaaaaaaaaaaaaa",
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache"
            assert artifact.build_id == build_id
            assert artifact.path == str(cached_isf)

    # ------------------------------------------------------------------
    # run_vol3_symbol_build (live-guest path)
    # ------------------------------------------------------------------

    def _make_live_job(self, in_memory_session_scope, kernel_release="6.8.0-101-generic"):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "running",
                detected_kernel_release=kernel_release,
                detected_distro="ubuntu",
                detected_version="22.04",
            )
            repo.upsert_runtime(
                job.id,
                "target",
                vm_name="test-vm",
                ip_address="192.168.1.10",
                state="running",
                power_state="on",
                capture_memory_enabled=True,
                capture_disk_enabled=False,
            )
            return job.id

    def test_live_build_id_mismatch_triggers_rebuild(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_symbol_build: probe returns different build_id than cache → live script runs."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        # File cache has a valid ISF.
        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_live_job(in_memory_session_scope)

        # Record the cached ISF with build_id A in the DB (from some other job).
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id="aaaaaaaaaaaaaaaa",
            )

        # Probe returns build_id B (current kernel has a different build).
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: "bbbbbbbbbbbbbbbb")

        # Track whether the live script was invoked.
        script_called = []

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            script_called.append(cmd)
            rebuilt = symbols_dir / "linux" / "rebuilt.json"
            rebuilt.write_text(self._VALID_ISF, encoding="utf-8")
            stdout.write(f"isf_path={rebuilt}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            stdout.write(b"build_id=bbbbbbbbbbbbbbbb\n")
            return MagicMock(returncode=0)

        from unittest.mock import patch

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert script_called, "Expected the live script to be called on build_id mismatch"
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "live_guest"
            assert artifact.build_id == "bbbbbbbbbbbbbbbb"
            assert artifact.path != str(cached_isf)

    def test_live_probe_failure_is_soft_hit(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_symbol_build: probe returns None (SSH fail, no readelf) → soft-hit."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))

        # File cache has a valid ISF.
        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_live_job(in_memory_session_scope)

        # Probe fails (returns None).
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: None)

        # The live script must NOT be called.
        script_called = []

        def fail_if_called(*a, **k):  # pragma: no cover
            script_called.append(a)

        monkeypatch.setattr(v3.subprocess, "run", fail_if_called)

        result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert not script_called, "Live script should not run on probe failure (soft-hit)"

    def test_live_build_uses_build_id_qualified_cache_hit(self, in_memory_session_scope, monkeypatch, tmp_path):
        """run_vol3_symbol_build: probed build_id finds a qualified-only cache file."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake", encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        build_id = "abcdef1234567890"
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: build_id)

        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / f"6.8.0-101-generic_{build_id}.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")
        stale_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        stale_isf.write_text(self._VALID_ISF, encoding="utf-8")

        job_id = self._make_live_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id=build_id,
            )
            newer_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                newer_job.id,
                "6.8.0-101-generic",
                str(stale_isf),
                "host_fetch",
                build_id="1111111111111111",
            )

        script_called = []

        def fail_if_called(*args, **kwargs):  # pragma: no cover
            script_called.append(args)
            raise AssertionError("qualified cache hit should not run live script")

        monkeypatch.setattr(v3.subprocess, "run", fail_if_called)

        result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        assert not script_called

    def test_isf_only_cache_hit_records_build_id_and_sha256(self, in_memory_session_scope, monkeypatch, tmp_path):
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
            ),
        )

        symbols_dir = tmp_path / "symbols"
        cached_isf = symbols_dir / "linux" / "6.8.0-101-generic_bbbbbbbbbbbbbbbb.json"
        cached_isf.parent.mkdir(parents=True)
        cached_isf.write_text(self._VALID_ISF, encoding="utf-8")

        autospec_setattr(monkeypatch, v3, "_symbol_path", lambda kernel_release, build_id=None: cached_isf)

        job_id = self._make_isf_only_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            repo.create_isf_artifact(
                job_id,
                "6.8.0-101-generic",
                str(cached_isf),
                "live_guest",
                build_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True, summary
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache"
            assert artifact.build_id == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            assert artifact.sha256 is not None

    def test_live_under_lock_cache_hit_records_build_id_and_sha256(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake", encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        live_build_id = "1234567890abcdef"
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: live_build_id)

        symbols_dir = tmp_path / "symbols"
        live_isf = tmp_path / "live.json"
        live_isf.write_text(self._VALID_ISF, encoding="utf-8")
        cached_isf = symbols_dir / "linux" / f"6.8.0-101-generic_{live_build_id}.json"

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            cached_isf.parent.mkdir(parents=True, exist_ok=True)
            cached_isf.write_text(self._VALID_ISF, encoding="utf-8")
            stdout.write(f"isf_path={live_isf}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            stdout.write(f"build_id={live_build_id}\n".encode())
            return MagicMock(returncode=0)

        job_id = self._make_live_job(in_memory_session_scope)

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "cache"
            assert artifact.build_id == live_build_id
            assert artifact.sha256 is not None

    def test_live_under_lock_ignores_legacy_cache_when_build_id_known(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake", encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        live_build_id = "abcdef1234567890"
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: live_build_id)

        symbols_dir = tmp_path / "symbols"
        legacy_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        legacy_isf.parent.mkdir(parents=True)
        legacy_isf.write_text(self._VALID_ISF, encoding="utf-8")
        live_isf = tmp_path / "live.json"
        live_isf.write_text(self._VALID_ISF, encoding="utf-8")

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            stdout.write(f"isf_path={live_isf}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            stdout.write(f"build_id={live_build_id}\n".encode())
            return MagicMock(returncode=0)

        job_id = self._make_live_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(legacy_isf),
                "live_guest",
                build_id="1111111111111111",
            )

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "live_guest"
            assert artifact.build_id == live_build_id
            assert artifact.path != str(legacy_isf)

    def test_live_under_lock_uses_probed_build_id_when_script_omits_it(
        self, in_memory_session_scope, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        autospec_setattr(monkeypatch, v3, "target_by_id", lambda tid: {"id": tid, "default_username": "ubuntu"})
        monkeypatch.setattr(
            v3,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        key = tmp_path / "id_rsa"
        key.write_text("fake", encoding="utf-8")
        autospec_setattr(monkeypatch, v3, "get_active_key_paths", lambda: (key, key))
        probed_build_id = "fedcba0987654321"
        autospec_setattr(monkeypatch, v3, "_probe_guest_build_id", lambda ip, key, user: probed_build_id)

        symbols_dir = tmp_path / "symbols"
        legacy_isf = symbols_dir / "linux" / "6.8.0-101-generic.json"
        legacy_isf.parent.mkdir(parents=True)
        legacy_isf.write_text(self._VALID_ISF, encoding="utf-8")
        live_isf = tmp_path / "live.json"
        live_isf.write_text(self._VALID_ISF, encoding="utf-8")

        def fake_run(cmd, *, stdout, stderr, timeout, env=None):
            stdout.write(f"isf_path={live_isf}\n".encode())
            stdout.write(b"kernel_release=6.8.0-101-generic\n")
            return MagicMock(returncode=0)

        job_id = self._make_live_job(in_memory_session_scope)
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            other_job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.create_isf_artifact(
                other_job.id,
                "6.8.0-101-generic",
                str(legacy_isf),
                "live_guest",
                build_id="1111111111111111",
            )

        with patch("astinuslab.forensics.vol3_support.subprocess.run", side_effect=fake_run):
            result = v3.run_vol3_symbol_build(job_id)

        assert result is True
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            artifact = repo.get_latest_isf_artifact(job_id)
            assert artifact is not None
            assert artifact.source == "live_guest"
            assert artifact.build_id == probed_build_id
            assert artifact.path != str(legacy_isf)

    source = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "astinuslab"
        / "orchestration"
        / "runners"
        / "boot_capture_runner.py"
    ).read_text(encoding="utf-8")

    assert "Generating ISF from live guest..." not in source
    assert "Generating Volatility 3 ISF..." in source


class TestProviderChainOutcome:
    def test_provider_attempt_outcome_serializes_to_event_dict(self):
        from astinuslab.forensics.symbols.base import IsfBuildOutcome, KernelMetadata, ProviderAttempt
        from astinuslab.forensics.symbols.identity import SymbolIdentity

        metadata = KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc")
        identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")
        outcome = IsfBuildOutcome(
            False,
            None,
            identity,
            metadata,
            [ProviderAttempt(name="debuginfod", error="missing")],
        )

        event = outcome.to_event_dict()

        assert event["success"] is False
        assert event["build_id"] == "abc"
        assert event["attempts"][0]["name"] == "debuginfod"

    def test_ordered_providers_chain_ends_at_host_fetch(self):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import KernelMetadata

        names = [p.name for p in v3._ordered_providers(KernelMetadata("ubuntu", "24.04", "6.8.0-test"))]

        assert names == ["local_cache", "prebuilt_isf", "debuginfod", "host_fetch"]

    def test_provider_chain_order_and_failed_attempts(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        calls = []

        class Provider:
            def __init__(self, name):
                self.name = name

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                calls.append(self.name)
                return FetchResult(success=False, error=f"{self.name} failed")

        autospec_setattr(
            monkeypatch,
            v3,
            "_ordered_providers",
            lambda metadata, guest_target=None, **_kwargs: [
                Provider(name)
                for name in [
                    "local_cache",
                    "prebuilt_isf",
                    "debuginfod",
                    "host_fetch",
                ]
            ],
        )
        autospec_setattr(monkeypatch, v3, "_log_chain_finished", lambda job_id, outcome: None)

        outcome = v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id="job-1",
        )

        assert calls == ["local_cache", "prebuilt_isf", "debuginfod", "host_fetch"]
        assert outcome.success is False
        assert [attempt.name for attempt in outcome.attempts] == calls
        assert all(attempt.error for attempt in outcome.attempts)

    def test_chain_records_vmlinux_path_on_attempt(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        class Provider:
            name = "host_fetch"

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                return FetchResult(success=True, vmlinux_path=tmp_path / "vmlinux")

        autospec_setattr(monkeypatch, v3, "_ordered_providers", lambda metadata, **_kwargs: [Provider()])
        autospec_setattr(monkeypatch, v3, "_build_isf_from_vmlinux", lambda result, identity, work_dir: None)
        autospec_setattr(monkeypatch, v3, "_log_chain_finished", lambda job_id, outcome: None)

        outcome = v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id="job-x",
        )

        assert outcome.success is False
        attempt = next(a for a in outcome.attempts if a.name == "host_fetch")
        assert attempt.vmlinux_path == str(tmp_path / "vmlinux")
        assert outcome.to_event_dict()["attempts"][0]["vmlinux_path"] == str(tmp_path / "vmlinux")

    def test_chain_surfaces_dwarf2json_readiness_code_on_vmlinux_provider(self, monkeypatch, tmp_path):
        """A vmlinux-returning provider must report WHY dwarf2json could not run.

        local_cache and prebuilt hand back a ready ISF, but debuginfod and host_fetch
        return a vmlinux, so they hard-require dwarf2json.  When it is unready the
        operator has already paid for a multi-hundred-MB download; the stable
        readiness code is the one thing that tells them what to fix, so it must reach
        the attempt error instead of dying in the server log behind a generic message
        naming two unrelated causes.
        """
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        class Provider:
            name = "debuginfod"

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                vmlinux_path = tmp_path / "vmlinux"
                vmlinux_path.write_bytes(b"\x7fELF")
                return FetchResult(success=True, vmlinux_path=vmlinux_path)

        autospec_setattr(monkeypatch, v3, "_ordered_providers", lambda metadata, **_kwargs: [Provider()])
        autospec_setattr(monkeypatch, v3, "_log_chain_finished", lambda job_id, outcome: None)
        monkeypatch.setattr(
            v3,
            "require_dwarf2json_tool",
            lambda _settings: (_ for _ in ()).throw(
                ToolDependencyError("dwarf2json_hash_mismatch", "The configured dwarf2json file changed.")
            ),
            raising=False,
        )
        monkeypatch.setattr(
            v3.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("dwarf2json must not spawn when it is unready"),
        )

        outcome = v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id="job-dwarf",
        )

        assert outcome.success is False
        attempt = next(a for a in outcome.attempts if a.name == "debuginfod")
        assert attempt.error is not None
        assert "dwarf2json_hash_mismatch" in attempt.error
        # The generic string belongs to genuine ISF-validation failures only; it must
        # not stand in for a dependency the operator can actually fix.
        assert attempt.error != "ISF validation failed or dwarf2json failed"
        assert "dwarf2json_hash_mismatch" in outcome.to_event_dict()["attempts"][0]["error"]

    def test_chain_keeps_generic_error_when_dwarf2json_ran_but_isf_was_invalid(self, monkeypatch, tmp_path):
        """A dwarf2json that ran and produced an unusable ISF is a different cause.

        It has no readiness code to report, so it keeps the generic message -- the two
        failure modes must stay distinguishable in the attempt record.
        """
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        class Provider:
            name = "host_fetch"

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                return FetchResult(success=True, vmlinux_path=tmp_path / "vmlinux")

        autospec_setattr(monkeypatch, v3, "_ordered_providers", lambda metadata, **_kwargs: [Provider()])
        autospec_setattr(monkeypatch, v3, "_build_isf_from_vmlinux", lambda result, identity, work_dir: None)
        autospec_setattr(monkeypatch, v3, "_log_chain_finished", lambda job_id, outcome: None)

        outcome = v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id="job-invalid-isf",
        )

        assert outcome.success is False
        attempt = next(a for a in outcome.attempts if a.name == "host_fetch")
        assert attempt.error == "ISF validation failed or dwarf2json failed"

    def test_build_id_resolved_from_sidecar_before_chain(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache
        from astinuslab.forensics.symbols.identity import SymbolIdentity
        from astinuslab.images.image_inspection import ImageMetadata, save_sidecar

        image = tmp_path / "img.qcow2"
        image.write_bytes(b"")
        save_sidecar(
            image,
            ImageMetadata(
                schema_version=1,
                distro="ubuntu",
                version="24.04",
                family="debian",
                default_username="ubuntu",
                os_variant="ubuntu24.04",
                machine_type=None,
                kernel_release="6.8.0-test",
                arch="x86_64",
                cloud_init_profile=None,
                inject_network_config=None,
                source="inspected",
                detected_at="2026-05-07T00:00:00+00:00",
                build_id="sidecar123",
            ),
        )

        class Provider:
            name = "local_cache"

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                return FetchResult(success=False, error="miss")

        autospec_setattr(
            monkeypatch, v3, "_ordered_providers", lambda metadata, guest_target=None, **_kwargs: [Provider()]
        )
        autospec_setattr(monkeypatch, v3, "_log_chain_finished", lambda job_id, outcome: None)

        outcome = v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id="job-1",
            image_path=image,
        )

        assert isinstance(outcome.metadata, KernelMetadata)
        assert isinstance(outcome.identity, SymbolIdentity)
        assert outcome.metadata.build_id == "sidecar123"
        assert outcome.identity.build_id == "sidecar123"

    def test_chain_finished_event_logged_on_failure(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            job_id = job.id

        class Provider:
            name = "local_cache"

            def supports(self, metadata):
                return True

            def skip_reason(self, metadata):
                return None

            def fetch_vmlinux(self, metadata, work_dir):
                return FetchResult(success=False, error="miss")

        autospec_setattr(
            monkeypatch, v3, "_ordered_providers", lambda metadata, guest_target=None, **_kwargs: [Provider()]
        )

        v3._run_symbol_provider_chain(
            KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="abc"),
            work_dir=tmp_path / "work",
            cache=SymbolCache(tmp_path / "cache", max_bytes=10_000_000),
            job_id=job_id,
        )

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            events = [(event.state, event.message) for event in repo.list_events(job_id)]

        assert any(state == "isf_chain_finished" and '"success": false' in message for state, message in events)

    def test_dwarf2json_declines_btf_source_without_spawning(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult
        from astinuslab.forensics.symbols.identity import SymbolIdentity

        monkeypatch.setattr(v3, "settings", SimpleNamespace(vol3_dwarf2json_path=tmp_path / "dwarf2json"))
        monkeypatch.setattr(
            v3.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("BTF-only input must not spawn dwarf2json"),
        )

        btf_path = tmp_path / "vmlinux.btf"
        btf_path.write_bytes(b"\x9f\xeb\x01\x00")
        result = FetchResult(success=True, vmlinux_path=btf_path, source_kind="btf")
        identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "live_guest")

        assert v3._build_isf_from_vmlinux(result, identity, tmp_path / "work") is None


class TestHostFetchBuildIdTrust:
    def test_host_fetch_unverified_when_guest_build_id_unknown(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import KernelMetadata

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF stub")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda path: "deadbeefcafe1234")

        metadata = KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id=None)
        result = v3.HostFetchProvider().fetch_vmlinux(metadata, tmp_path / "work")

        assert result.success is True
        assert result.build_id_verified is False
        assert metadata.build_id == "deadbeefcafe1234"

    def test_host_fetch_verified_when_build_id_matches(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import KernelMetadata

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF stub")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda path: "deadbeefcafe1234")

        metadata = KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="deadbeefcafe1234")
        result = v3.HostFetchProvider().fetch_vmlinux(metadata, tmp_path / "work")

        assert result.success is True
        assert result.build_id_verified is True

    def test_host_fetch_unverified_when_build_id_unextractable(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import KernelMetadata

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF stub")
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda path: None)

        metadata = KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id="deadbeefcafe1234")
        result = v3.HostFetchProvider().fetch_vmlinux(metadata, tmp_path / "work")

        assert result.success is True
        assert result.build_id_verified is False

    def test_chain_stamps_unverified_source_on_artifact(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3
        from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata
        from astinuslab.forensics.symbols.cache import SymbolCache

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF stub")
        built_isf = tmp_path / "built-isf.json"
        built_isf.write_text(
            '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}',
            encoding="utf-8",
        )

        autospec_setattr(
            monkeypatch,
            v3.PrebuiltIsfProvider,
            "fetch_vmlinux",
            lambda self, metadata, work_dir: FetchResult(success=False, error="miss"),
        )
        autospec_setattr(
            monkeypatch,
            v3.DebuginfodProvider,
            "fetch_vmlinux",
            lambda self, metadata, work_dir: FetchResult(success=False, error="miss"),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *args, **kwargs: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda path: "deadbeefcafe1234")
        autospec_setattr(monkeypatch, v3, "_build_isf_from_vmlinux", lambda result, identity, work_dir: built_isf)

        cache = SymbolCache((tmp_path / "symbols").resolve(), max_bytes=10_000_000)
        metadata = KernelMetadata("ubuntu", "24.04", "6.8.0-test", build_id=None)

        outcome = v3._run_symbol_provider_chain(
            metadata,
            work_dir=tmp_path / "work",
            cache=cache,
            job_id="job-test01",
        )

        assert outcome.success is True
        assert outcome.identity.source == "host_fetch_unverified"
        assert outcome.isf_path is not None
        assert "host_fetch_unverified" in outcome.isf_path.name


class TestValidateIsf:
    def test_rejects_missing_file(self, tmp_path):
        from pathlib import Path

        import astinuslab.forensics.vol3_support as v3

        ok, reason = v3._validate_isf(Path("/nonexistent.json"))
        assert ok is False
        assert reason is not None

    def test_rejects_zero_length_file(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "empty.json"
        f.write_bytes(b"")
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert reason is not None

    def test_rejects_invalid_json(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "bad.json"
        f.write_bytes(b"not json")
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert reason is not None

    def test_rejects_empty_dict(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "empty_dict.json"
        f.write_bytes(b"{}")
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert reason is not None

    def test_rejects_missing_metadata(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "missing_metadata.json"
        f.write_bytes(b'{"symbols": {"init_task": {}}}')
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert "metadata.linux" in str(reason)

    def test_rejects_missing_linux_metadata(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "missing_linux.json"
        f.write_bytes(b'{"metadata": {}, "symbols": {"init_task": {}}}')
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert "metadata.linux" in str(reason)

    def test_rejects_missing_symbols(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "missing_symbols.json"
        f.write_bytes(b'{"metadata": {"linux": {"name": "Linux"}}}')
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert "symbols" in str(reason)

    def test_rejects_empty_symbols(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "empty_symbols.json"
        f.write_bytes(b'{"metadata": {"linux": {"name": "Linux"}}, "symbols": {}}')
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert "symbols" in str(reason)

    def test_rejects_json_array(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "array.json"
        f.write_bytes(b'[{"type": "linux"}]')
        ok, reason = v3._validate_isf(f)
        assert ok is False
        assert reason is not None

    def test_accepts_dwarf2json_linux_isf_shape(self, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        f = tmp_path / "valid.json"
        f.write_bytes(b'{"metadata": {"linux": {"name": "Linux"}}, "symbols": {"init_task": {}}}')
        ok, reason = v3._validate_isf(f)
        assert ok is True
        assert reason is None


class TestFinalise:
    def test_skips_db_writes_when_job_deleted(self, in_memory_session_scope, monkeypatch):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        # job-ghost does not exist in DB — simulates delete-during-analysis race
        v3._finalise("job-ghost", action_id=None, success=True, summary="done")
        # No exception means the guard worked correctly


def test_run_vol3_symbol_build_passes_vmlinux_override(tmp_path, monkeypatch):
    """When called with vmlinux_override, the guest script env contains VMLINUX_OVERRIDE
    and the path is scp'd to the guest first."""
    from pathlib import Path
    from types import SimpleNamespace

    from astinuslab.forensics import vol3_support

    scp_calls: list[tuple[object, object]] = []
    subprocess_env: object = None

    def fake_subprocess_run(cmd, **kw):
        nonlocal subprocess_env
        subprocess_env = kw.get("env")
        return SimpleNamespace(returncode=0)

    def fake_scp(local_path, ip_address, ssh_key, guest_user):
        scp_calls.append((local_path, ip_address))
        return f"/tmp/astinuslab-vmlinux-{Path(local_path).name}"

    monkeypatch.setattr(
        vol3_support, "subprocess", SimpleNamespace(run=fake_subprocess_run, TimeoutExpired=TimeoutError)
    )
    monkeypatch.setattr(vol3_support, "_scp_vmlinux_to_guest", fake_scp, raising=False)

    host_vmlinux = tmp_path / "vmlinux-6.14.0-36-generic"
    host_vmlinux.write_bytes(b"\x7fELF stub")

    autospec_setattr(monkeypatch, vol3_support, "get_active_key_paths", lambda: (Path("/tmp/k"), Path("/tmp/p")))
    autospec_setattr(monkeypatch, vol3_support, "_target_guest_user", lambda tid: "ubuntu")

    class FakeRepo:
        def get_runtime(self, *a, **k):
            return SimpleNamespace(ip_address="10.0.0.5")

        def get_job(self, *a, **k):
            return SimpleNamespace(
                detected_kernel_release="6.14.0-36-generic",
                detected_distro="ubuntu",
                detected_version="25.04",
                target_id="t1",
            )

    autospec_setattr(monkeypatch, vol3_support, "JobRepository", lambda s: FakeRepo())
    import contextlib

    autospec_setattr(monkeypatch, vol3_support, "session_scope", lambda: contextlib.nullcontext(enter_result=None))

    vol3_support.run_vol3_symbol_build(
        "job-test",
        log_name="symbol-build-live.log",
        vmlinux_override=str(host_vmlinux),
    )
    assert scp_calls, "vmlinux was not scp'd to guest"
    assert isinstance(subprocess_env, dict)
    assert "VMLINUX_OVERRIDE" in subprocess_env


class TestSymbolSlugAndCacheKey:
    """ISF-01: verify build_id-qualified slugs prevent cache collisions."""

    _VALID_ISF = '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'

    def test_valid_hex_build_id_appends_suffix(self):
        from astinuslab.forensics.vol3_support import _symbol_slug

        slug = _symbol_slug("6.1.0-18-amd64", "aabbccdd00112233")
        assert slug.endswith("_aabbccdd00112233")

    def test_build_id_truncated_to_16_chars(self):
        from astinuslab.forensics.vol3_support import _symbol_slug

        slug = _symbol_slug("6.1.0-18-amd64", "aabbccdd001122334455")
        assert slug.endswith("_aabbccdd00112233"), slug

    def test_no_build_id_gives_unqualified_slug(self):
        from astinuslab.forensics.vol3_support import _symbol_slug

        assert _symbol_slug("6.1.0-18-amd64") == "6.1.0-18-amd64"
        assert _symbol_slug("6.1.0-18-amd64", None) == "6.1.0-18-amd64"

    def test_non_hex_build_id_gives_unqualified_slug(self):
        from astinuslab.forensics.vol3_support import _symbol_slug

        unq = _symbol_slug("6.1.0-18-amd64")
        assert _symbol_slug("6.1.0-18-amd64", "not-valid-hex-xyz") == unq
        assert _symbol_slug("6.1.0-18-amd64", "feedface99") == unq

    def test_different_build_ids_produce_different_slugs(self):
        from astinuslab.forensics.vol3_support import _symbol_slug

        slug_a = _symbol_slug("6.1.0-18-amd64", "aabbccdd00112233")
        slug_b = _symbol_slug("6.1.0-18-amd64", "ffeeddcc99887766")
        assert slug_a != slug_b

    def test_two_builds_same_kernel_release_do_not_share_file(self, tmp_path):
        """ISF-01 regression: two kernel builds with same uname -r must use distinct paths."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        SimpleNamespace(vol3_symbols_path=tmp_path)
        linux_dir = tmp_path / "linux"
        linux_dir.mkdir()

        bid_a = "aabbccdd00112233"
        bid_b = "ffeeddcc99887766"
        kr = "6.1.0-18-amd64"

        slug_a = v3._symbol_slug(kr, bid_a)
        slug_b = v3._symbol_slug(kr, bid_b)
        assert slug_a != slug_b, "slugs must differ for different build_ids"

        path_a = linux_dir / f"{slug_a}.json"
        path_b = linux_dir / f"{slug_b}.json"
        path_a.write_text(self._VALID_ISF)
        path_b.write_text('{"metadata":{"linux":{"name":"Linux2"}},"symbols":{"init_task":{}}}')

        # Both files must coexist — neither overwrites the other
        assert path_a.exists() and path_b.exists()
        assert "Linux2" not in path_a.read_text()
        assert "Linux2" in path_b.read_text()

    def test_unqualified_slug_is_soft_hit_when_qualified_not_found(
        self, in_memory_session_scope, tmp_path, monkeypatch
    ):
        """ISF-01: backward compat — old unqualified ISF is found when no qualified file exists."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "settings", SimpleNamespace(vol3_symbols_path=tmp_path))
        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)

        linux_dir = tmp_path / "linux"
        linux_dir.mkdir()
        unq = linux_dir / "6.1.0-18-amd64.json"
        unq.write_text(self._VALID_ISF)

        # With a valid hex build_id that has no matching qualified file, the
        # active cache lookup must preserve the unqualified file as a soft hit.
        assert v3._cached_symbol_path_for_job("6.1.0-18-amd64", "aabbccdd00112233") == (unq, True)
        assert v3._cached_symbol_path_for_job("6.1.0-18-amd64", None) == (unq, True)

    def test_qualified_slug_takes_priority_over_unqualified(self, tmp_path, monkeypatch):
        """When a qualified ISF exists it is preferred over the unqualified one."""
        from types import SimpleNamespace

        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "settings", SimpleNamespace(vol3_symbols_path=tmp_path))

        linux_dir = tmp_path / "linux"
        linux_dir.mkdir()
        bid = "aabbccdd00112233"
        kr = "6.1.0-18-amd64"

        unq = linux_dir / f"{v3._symbol_slug(kr)}.json"
        qual = linux_dir / f"{v3._symbol_slug(kr, bid)}.json"
        unq.write_text(self._VALID_ISF)
        qual.write_text(self._VALID_ISF)

        found = v3._symbol_path(kr, bid)
        assert found == qual, f"expected qualified path {qual}, got {found}"


# ---------------------------------------------------------------------------
# ISF-3: host dwarf2json subprocess timeout
# ---------------------------------------------------------------------------


class TestHostDwarf2jsonTimeout:
    _VALID_ISF = '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'

    def _make_job(self, in_memory_session_scope, kernel_release="6.8.0-101-generic"):
        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_distro="ubuntu",
                detected_version="22.04",
                detected_kernel_release=kernel_release,
            )
            return job.id

    def test_host_dwarf2json_timeout_falls_back_to_live_guest(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *a, **k: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda _p: None)

        fallback_calls = []

        def fake_fallback(job_id, *, log_name, vmlinux_override=None):
            fallback_calls.append({"job_id": job_id, "vmlinux_override": vmlinux_override})
            return True

        monkeypatch.setattr(v3, "run_vol3_symbol_build", fake_fallback)

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["dwarf2json"], timeout=600)
            job_id = self._make_job(in_memory_session_scope)
            success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        assert "timeout" in summary.lower() or "timed out" in summary.lower()
        assert fallback_calls, "live-guest fallback should have been invoked"
        assert fallback_calls[0]["vmlinux_override"] == str(vmlinux)
        # Temp ISF file must be cleaned up
        leftover = (
            list((tmp_path / "symbols" / "linux").glob("*.tmp")) if (tmp_path / "symbols" / "linux").exists() else []
        )
        assert leftover == [], f"temp ISF files not cleaned up: {leftover}"

    def test_host_dwarf2json_timeout_uses_settings_value(self, in_memory_session_scope, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")
        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=tmp_path / "symbols",
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=42,
            ),
        )
        autospec_setattr(monkeypatch, v3, "_run_fetch_vmlinux", lambda *a, **k: (str(vmlinux), None))
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda _p: None)
        autospec_setattr(monkeypatch, v3, "run_vol3_symbol_build", lambda *a, **k: False)

        with patch("astinuslab.forensics.vol3_support.subprocess.run") as mock_run:
            mock_run.return_value = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(returncode=1)
            job_id = self._make_job(in_memory_session_scope)
            v3.run_vol3_isf_only(job_id)

        assert mock_run.called
        called_timeout = mock_run.call_args.kwargs.get("timeout")
        assert called_timeout == 42, f"expected timeout=42, got {called_timeout}"


# ---------------------------------------------------------------------------
# ISF-7: cooperative cache flock per slug
# ---------------------------------------------------------------------------


class TestIsfCacheLock:
    _VALID_ISF = '{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}'

    def test_lock_file_is_persistent_after_release(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        with v3._isf_cache_lock("test-slug"):
            pass
        lock_file = tmp_path / "symbols" / "linux" / ".lock-test-slug"
        assert lock_file.exists(), "lock file should persist after release (cooperative semantics)"

    def test_lock_file_path_uses_slug(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                vol3_symbols_path=tmp_path / "symbols",
            ),
        )
        with v3._isf_cache_lock("my-kernel-6.8.0"):
            pass
        assert (tmp_path / "symbols" / "linux" / ".lock-my-kernel-6.8.0").exists()

    def test_lock_recheck_short_circuits_on_concurrent_winner(self, in_memory_session_scope, monkeypatch, tmp_path):
        """Single-thread race simulation: ISF appears between lock acquire and re-check."""
        from contextlib import contextmanager

        import astinuslab.forensics.vol3_support as v3

        kernel_release = "6.8.0-101-generic"
        symbols_dir = tmp_path / "symbols"
        isf_path = symbols_dir / "linux" / f"{v3._symbol_slug(kernel_release)}.json"

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )

        # Replace _isf_cache_lock to drop the ISF into the cache before yielding,
        # simulating a concurrent winner that finished just before we acquired the lock.
        @contextmanager
        def winning_lock(slug):
            isf_path.parent.mkdir(parents=True, exist_ok=True)
            isf_path.write_text(self._VALID_ISF)
            yield

        monkeypatch.setattr(v3, "_isf_cache_lock", winning_lock)

        fetch_called: list[int] = []

        def fake_fetch_vmlinux(*a, **k):
            fetch_called.append(1)
            return None, None

        monkeypatch.setattr(v3, "_run_fetch_vmlinux", fake_fetch_vmlinux)

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(
                job.id,
                "completed",
                detected_distro="ubuntu",
                detected_version="22.04",
                detected_kernel_release=kernel_release,
            )
            job_id = job.id

        success, summary = v3.run_vol3_isf_only(job_id)

        assert success is True
        assert not fetch_called, "_run_fetch_vmlinux must not be called when re-check hits"

    def test_serializes_concurrent_builders_for_same_kernel(self, in_memory_session_scope, monkeypatch, tmp_path):
        """Two threads for the same kernel: only one invokes dwarf2json (the second hits cache)."""
        import threading
        import time

        import astinuslab.forensics.vol3_support as v3

        # Keep the new platform lookup out of this subprocess/locking test.
        monkeypatch.setattr(v3, "target_by_id", lambda _target_id: {"distro": "ubuntu"})

        kernel_release = "6.8.0-101-generic"
        symbols_dir = tmp_path / "symbols"
        symbols_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(v3, "session_scope", in_memory_session_scope)
        monkeypatch.setattr(
            v3,
            "settings",
            __import__("types").SimpleNamespace(
                storage_dir=tmp_path / "storage",
                vol3_symbols_path=symbols_dir,
                vol3_dwarf2json_path=tmp_path / "dwarf2json",
                vol3_dwarf2json_timeout_seconds=600,
            ),
        )
        autospec_setattr(monkeypatch, v3, "_extract_build_id", lambda _p: None)

        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF")

        def fake_fetch(*a, **k):
            return str(vmlinux), None

        monkeypatch.setattr(v3, "_run_fetch_vmlinux", fake_fetch)

        dwarf2json_calls = []

        def fake_dwarf2json(cmd, *, stdout, stderr, timeout=None, **kw):
            dwarf2json_calls.append(1)
            time.sleep(0.15)  # widen the race window so the second thread reaches the lock
            stdout.write(self._VALID_ISF.encode())
            return __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(returncode=0)

        monkeypatch.setattr(v3.subprocess, "run", fake_dwarf2json)

        def make_job():
            with in_memory_session_scope() as session:
                repo = JobRepository(session)
                job = repo.create_job(
                    JobCreate(
                        target_id="ubuntu-22-04-default",
                        scenario_id="immediate-capture",
                        execution_mode="immediate",
                    )
                )
                repo.force_transition(
                    job.id,
                    "completed",
                    detected_distro="ubuntu",
                    detected_version="22.04",
                    detected_kernel_release=kernel_release,
                )
                return job.id

        job_a = make_job()
        job_b = make_job()

        results = {}

        def run_a():
            results["a"] = v3.run_vol3_isf_only(job_a)

        def run_b():
            time.sleep(0.02)  # slight delay so A enters the lock first
            results["b"] = v3.run_vol3_isf_only(job_b)

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert results.get("a", (False,))[0] is True, f"thread A failed: {results.get('a')}"
        assert results.get("b", (False,))[0] is True, f"thread B failed: {results.get('b')}"
        assert len(dwarf2json_calls) == 1, f"dwarf2json should be called exactly once; got {len(dwarf2json_calls)}"


# ---------------------------------------------------------------------------
# Local dwarf2json is freshly verified at the execution boundary
# ---------------------------------------------------------------------------


def test_local_isf_build_fresh_checks_and_uses_resolved_dwarf2json(monkeypatch, tmp_path):
    import astinuslab.forensics.vol3_support as v3
    from astinuslab.forensics.symbols.base import FetchResult
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    resolved = tmp_path / "replacement-dwarf2json"
    events: list[Any] = []

    def require(_settings):
        events.append("require")
        return resolved

    def fake_run(cmd, **kwargs):
        events.append(("spawn", cmd))
        kwargs["stdout"].write(b'{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}')
        return MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr(
        v3,
        "settings",
        SimpleNamespace(
            vol3_dwarf2json_path=tmp_path / "queued-dwarf2json",
            vol3_dwarf2json_timeout_seconds=41,
        ),
    )
    monkeypatch.setattr(v3, "require_dwarf2json_tool", require, raising=False)
    monkeypatch.setattr(v3.subprocess, "run", fake_run)
    vmlinux_path = tmp_path / "vmlinux"
    vmlinux_path.write_bytes(b"\x7fELF")
    result = FetchResult(success=True, vmlinux_path=vmlinux_path, source_kind="elf")
    identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")

    built = v3._build_isf_from_vmlinux(result, identity, tmp_path / "work")

    assert built is not None
    assert events[0] == "require"
    assert events[1][0] == "spawn"
    assert events[1][1][0] == str(resolved)


def test_local_isf_build_removed_dwarf2json_fails_without_spawning(monkeypatch, tmp_path):
    """An unready dwarf2json aborts the build without spawning, carrying its code.

    The readiness code is the only actionable thing the operator gets, so it must
    escape the builder rather than be logged and flattened into a bare None.
    """
    import astinuslab.forensics.vol3_support as v3
    from astinuslab.forensics.symbols.base import FetchResult
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    monkeypatch.setattr(
        v3,
        "settings",
        SimpleNamespace(
            vol3_dwarf2json_path=tmp_path / "queued-dwarf2json",
            vol3_dwarf2json_timeout_seconds=41,
        ),
    )
    monkeypatch.setattr(
        v3,
        "require_dwarf2json_tool",
        lambda _settings: (_ for _ in ()).throw(
            ToolDependencyError("dwarf2json_hash_mismatch", "The configured dwarf2json file changed.")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        v3.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dwarf2json must not spawn after readiness changed"),
    )
    vmlinux_path = tmp_path / "vmlinux"
    vmlinux_path.write_bytes(b"\x7fELF")
    result = FetchResult(success=True, vmlinux_path=vmlinux_path, source_kind="elf")
    identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")

    with pytest.raises(ToolDependencyError) as excinfo:
        v3._build_isf_from_vmlinux(result, identity, tmp_path / "work")

    assert excinfo.value.code == "dwarf2json_hash_mismatch"


def test_legacy_dwarf2json_sidecar_verifier_and_cache_are_removed():
    import astinuslab.forensics.vol3_support as v3

    assert not hasattr(v3, "verify_dwarf2json_binary")
    assert not hasattr(v3, "_VERIFY_CACHE")


# ---------------------------------------------------------------------------
# ISF-9: post-scp shell script verification (shell-grep style)
# ---------------------------------------------------------------------------
