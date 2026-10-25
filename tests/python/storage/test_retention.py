"""Unit tests for the retention policy module."""

from __future__ import annotations

import time
from unittest.mock import patch

from astinuslab.storage.retention import _purge_old_files
from tests.python._support.helpers import autospec_setattr


class TestPurgeOldFiles:
    def test_deletes_files_older_than_cutoff(self, tmp_path):
        old_file = tmp_path / "old.dump"
        old_file.write_bytes(b"data")
        # Set mtime to 100 days ago
        past = time.time() - (100 * 86400)
        import os

        os.utime(old_file, (past, past))

        count = _purge_old_files(tmp_path, older_than_days=90)
        assert count == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self, tmp_path):
        recent = tmp_path / "recent.dump"
        recent.write_bytes(b"data")
        count = _purge_old_files(tmp_path, older_than_days=90)
        assert count == 0
        assert recent.exists()

    def test_returns_zero_for_missing_directory(self, tmp_path):
        missing = tmp_path / "nonexistent"
        assert _purge_old_files(missing, older_than_days=7) == 0

    def test_suffix_filter_skips_non_matching_files(self, tmp_path):
        import os

        old = tmp_path / "old.log"
        old.write_bytes(b"x")
        past = time.time() - (100 * 86400)
        os.utime(old, (past, past))
        count = _purge_old_files(tmp_path, older_than_days=90, suffix=".dump")
        assert count == 0
        assert old.exists()

    def test_suffix_filter_deletes_matching_files(self, tmp_path):
        import os

        old = tmp_path / "old.dump"
        old.write_bytes(b"x")
        past = time.time() - (100 * 86400)
        os.utime(old, (past, past))
        count = _purge_old_files(tmp_path, older_than_days=90, suffix=".dump")
        assert count == 1

    def test_skips_subdirectories(self, tmp_path):
        import os

        subdir = tmp_path / "subdir"
        subdir.mkdir()
        past = time.time() - (100 * 86400)
        os.utime(subdir, (past, past))
        count = _purge_old_files(tmp_path, older_than_days=90)
        assert count == 0

    def test_oserror_on_delete_is_non_fatal(self, tmp_path, monkeypatch):
        import os

        old = tmp_path / "locked.dump"
        old.write_bytes(b"x")
        past = time.time() - (100 * 86400)
        os.utime(old, (past, past))

        with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
            count = _purge_old_files(tmp_path, older_than_days=90)
        assert count == 0  # non-fatal, counted as not-deleted


class FakeSettings:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.data_dir = base_dir
        self.storage_dir = base_dir / "storage"
        self.serial_log_dir = base_dir / "storage" / "logs" / "serial"


def _patch_retention(monkeypatch, tmp_path, in_memory_session_scope):
    import astinuslab.storage.retention as ret_mod
    from astinuslab import app_settings as app_settings_mod

    monkeypatch.setattr(ret_mod, "settings", FakeSettings(tmp_path))
    monkeypatch.setattr(ret_mod, "session_scope", in_memory_session_scope)
    # run_retention records last-run metadata through app_settings; keep that write hermetic.
    monkeypatch.setattr(app_settings_mod, "session_scope", in_memory_session_scope)
    autospec_setattr(monkeypatch, ret_mod, "_get_retention_days", lambda: (90, 180))
    return ret_mod


class TestRunRetention:
    def test_returns_counts_dict(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)

        result = ret_mod.run_retention()
        assert "dumps_deleted" in result
        assert "logs_deleted" in result
        assert "serial_logs_deleted" in result
        assert "protected_skipped" in result


def _write_serial_log(tmp_path, name, *, age_days):
    import os

    serial_dir = tmp_path / "storage" / "logs" / "serial"
    serial_dir.mkdir(parents=True, exist_ok=True)
    path = serial_dir / name
    path.write_text("guest console output\n", encoding="utf-8")
    past = time.time() - (age_days * 86400)
    os.utime(path, (past, past))
    return path


class TestSerialLogRetention:
    """Per-job serial console logs live in a subdirectory the top-level sweep never enters."""

    def test_old_serial_logs_are_deleted(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        stale = _write_serial_log(tmp_path, "astinuslab-job-old-serial.log", age_days=400)

        result = ret_mod.run_retention()

        assert result["serial_logs_deleted"] == 1
        assert not stale.exists()

    def test_recent_serial_logs_are_kept(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        fresh = _write_serial_log(tmp_path, "astinuslab-job-live-serial.log", age_days=1)

        result = ret_mod.run_retention()

        assert result["serial_logs_deleted"] == 0
        assert fresh.exists()

    def test_serial_logs_are_counted_apart_from_the_top_level_sweep(
        self, tmp_path, monkeypatch, in_memory_session_scope
    ):
        import os

        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        logs_dir = tmp_path / "storage" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        rotated = logs_dir / "api.log.3"
        rotated.write_text("rotated\n", encoding="utf-8")
        past = time.time() - (400 * 86400)
        os.utime(rotated, (past, past))
        _write_serial_log(tmp_path, "astinuslab-job-old-serial.log", age_days=400)

        result = ret_mod.run_retention()

        assert result["logs_deleted"] == 1
        assert result["serial_logs_deleted"] == 1

    def test_dry_run_reports_without_deleting(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        stale = _write_serial_log(tmp_path, "astinuslab-job-old-serial.log", age_days=400)

        result = ret_mod.run_retention(dry_run=True)

        assert result["serial_logs_deleted"] == 1
        assert stale.exists()

    def test_candidate_preview_does_not_change_following_policy(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        recent = _write_serial_log(tmp_path, "astinuslab-candidate-serial.log", age_days=7)
        assert ret_mod.run_retention(dry_run=True, retention_days=(1, 2))["serial_logs_deleted"] == 1
        assert recent.exists()
        assert ret_mod.run_retention(dry_run=True)["serial_logs_deleted"] == 0

    def test_missing_serial_directory_is_not_an_error(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)

        assert ret_mod.run_retention()["serial_logs_deleted"] == 0

    def test_serial_deletions_are_included_in_the_recorded_log_total(
        self, tmp_path, monkeypatch, in_memory_session_scope
    ):
        """The Settings page shows one 'logs' figure; it must not under-report."""
        from astinuslab.app_settings import load_app_settings

        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        _write_serial_log(tmp_path, "astinuslab-job-old-serial.log", age_days=400)

        ret_mod.run_retention()

        assert load_app_settings()["retention"]["last_logs_deleted"] == 1


class TestEvidenceAwareRetention:
    def _make_capture_artifact(
        self,
        scope,
        tmp_path,
        *,
        age_days=120,
        job_status="completed",
        kind="memory",
        acquisition_id=None,
        case_id=None,
    ):
        from datetime import UTC, datetime, timedelta

        from astinuslab.api.schemas import JobCreate
        from astinuslab.db.repositories import ArtifactRepository, JobRepository

        dump_dir = tmp_path / "storage" / "dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)
        name = "memory.dump" if kind == "memory" else "ubuntu-22.04-kernel-6.8.0-disk.img.gz"
        artifact_path = dump_dir / name
        artifact_path.write_bytes(b"x")
        manifest = artifact_path.with_suffix(artifact_path.suffix + ".manifest.json")
        sidecar = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
        manifest.write_text("{}", encoding="utf-8")
        sidecar.write_text("deadbeef  " + name + "\n", encoding="utf-8")

        with scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.force_transition(job.id, job_status)
            if acquisition_id:
                job.acquisition_id = acquisition_id
            artifact = ArtifactRepository(session).create_artifact(
                kind=kind,
                producer="job_capture",
                path=str(artifact_path),
                filename=name,
                sha256="deadbeef",
                manifest_path=str(manifest),
                sha256_sidecar_path=str(sidecar),
                job_id=job.id,
                case_id=case_id,
            )
            artifact.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)
            artifact_id, job_id = artifact.id, job.id
        return artifact_id, job_id, artifact_path, manifest, sidecar

    def test_old_unaffiliated_artifact_is_fully_deleted(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        artifact_id, job_id, artifact_path, manifest, sidecar = self._make_capture_artifact(
            in_memory_session_scope, tmp_path
        )

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 1
        assert not artifact_path.exists()
        assert not manifest.exists()
        assert not sidecar.exists()
        from astinuslab.db.models import Artifact, JobEvent

        with in_memory_session_scope() as session:
            assert session.get(Artifact, artifact_id) is None
            events = session.query(JobEvent).filter(JobEvent.job_id == job_id).all()
            assert any(event.state == "retention_deleted" for event in events)

    def test_recent_artifact_is_kept(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        self._make_capture_artifact(in_memory_session_scope, tmp_path, age_days=10)

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert (tmp_path / "storage" / "dumps" / "memory.dump").exists()

    def test_artifact_in_acquisition_is_protected(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        from astinuslab.db.repositories import AcquisitionRepository

        with in_memory_session_scope() as session:
            acquisition = AcquisitionRepository(session).create_acquisition("case capture")
            acquisition_id = acquisition.id
        _, _, artifact_path, _, _ = self._make_capture_artifact(
            in_memory_session_scope, tmp_path, acquisition_id=acquisition_id
        )

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert result["protected_skipped"] == 1
        assert artifact_path.exists()

    def test_artifact_in_open_case_is_protected(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        from astinuslab.db.repositories import CaseRepository

        with in_memory_session_scope() as session:
            case = CaseRepository(session).create_case("open case")
            case_id = case.id
        _, _, artifact_path, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path, case_id=case_id)

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert result["protected_skipped"] == 1
        assert artifact_path.exists()

    def test_artifact_in_closed_case_is_deleted(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        from astinuslab.db.repositories import CaseRepository

        with in_memory_session_scope() as session:
            repo = CaseRepository(session)
            case = repo.create_case("closed case")
            case_id = case.id
            repo.update_case_status(case_id, "closed")
        _, _, artifact_path, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path, case_id=case_id)

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 1
        assert not artifact_path.exists()

    def test_non_terminal_job_is_protected(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        _, _, artifact_path, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path, job_status="running")

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert result["protected_skipped"] == 1
        assert artifact_path.exists()

    def test_disk_artifact_ages_out(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        _, _, artifact_path, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path, kind="disk")

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 1
        assert not artifact_path.exists()

    def test_missing_file_row_is_still_removed(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        artifact_id, _, artifact_path, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path)
        artifact_path.unlink()

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 1
        from astinuslab.db.models import Artifact

        with in_memory_session_scope() as session:
            assert session.get(Artifact, artifact_id) is None

    def test_path_outside_dump_roots_is_refused(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        artifact_id, _, _, _, _ = self._make_capture_artifact(in_memory_session_scope, tmp_path)
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        outside = outside_dir / "memory.dump"
        outside.write_bytes(b"x")
        from astinuslab.db.models import Artifact

        with in_memory_session_scope() as session:
            artifact = session.get(Artifact, artifact_id)
            assert artifact is not None
            artifact.path = str(outside)

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert result["protected_skipped"] == 1
        assert outside.exists()
        with in_memory_session_scope() as session:
            assert session.get(Artifact, artifact_id) is not None

    def test_sidecar_outside_artifact_dir_is_refused(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        artifact_id, _, artifact_path, manifest, sidecar = self._make_capture_artifact(
            in_memory_session_scope, tmp_path
        )
        outside = tmp_path / "do-not-delete.sha256"
        outside.write_text("keep", encoding="utf-8")
        from astinuslab.db.models import Artifact

        with in_memory_session_scope() as session:
            artifact = session.get(Artifact, artifact_id)
            assert artifact is not None
            artifact.sha256_sidecar_path = str(outside)

        result = ret_mod.run_retention()

        assert result["dumps_deleted"] == 0
        assert result["protected_skipped"] == 1
        assert artifact_path.exists()
        assert manifest.exists()
        assert sidecar.exists()
        assert outside.read_text(encoding="utf-8") == "keep"
        with in_memory_session_scope() as session:
            assert session.get(Artifact, artifact_id) is not None


class TestDryRunAndLastRun:
    def test_dry_run_counts_but_deletes_nothing(self, tmp_path, monkeypatch, in_memory_session_scope):
        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        artifact_id, _, artifact_path, manifest, sidecar = TestEvidenceAwareRetention()._make_capture_artifact(
            in_memory_session_scope, tmp_path
        )

        result = ret_mod.run_retention(dry_run=True)

        assert result["dumps_deleted"] == 1  # would delete
        assert artifact_path.exists()  # but did not
        assert manifest.exists()
        assert sidecar.exists()
        from astinuslab.db.models import Artifact

        with in_memory_session_scope() as session:
            assert session.get(Artifact, artifact_id) is not None

    def test_dry_run_does_not_record_last_run(self, tmp_path, monkeypatch, in_memory_session_scope):
        from astinuslab import app_settings as app_settings_mod

        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        ret_mod.run_retention(dry_run=True)

        assert "last_run_at" not in app_settings_mod.load_app_settings()["retention"]

    def test_real_run_records_last_run_metadata(self, tmp_path, monkeypatch, in_memory_session_scope):
        from astinuslab import app_settings as app_settings_mod

        ret_mod = _patch_retention(monkeypatch, tmp_path, in_memory_session_scope)
        TestEvidenceAwareRetention()._make_capture_artifact(in_memory_session_scope, tmp_path)

        ret_mod.run_retention()

        retention = app_settings_mod.load_app_settings()["retention"]
        assert retention["last_run_at"]  # ISO timestamp recorded
        assert retention["last_dumps_deleted"] == 1
        assert retention["last_logs_deleted"] == 0
