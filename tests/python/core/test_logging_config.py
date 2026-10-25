"""Logging configuration: the stdout contract, the optional rotating file, and log names."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest

from astinuslab.core import logging_config

pytestmark = pytest.mark.unit


@pytest.fixture()
def restore_root_handlers():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _astinuslab_handlers():
    owned = {logging_config._HANDLER_MARKER, logging_config._FILE_HANDLER_MARKER}
    return [h for h in logging.getLogger().handlers if getattr(h, "name", None) in owned]


class TestLogFileNames:
    def test_every_runtime_service_has_a_log_file(self):
        assert logging_config.SERVICE_LOG_FILES == {
            "api": "api.log",
            "runner": "runner.log",
            "analysis": "analysis-worker.log",
        }

    def test_log_file_names_matches_the_service_map(self):
        assert set(logging_config.LOG_FILE_NAMES) == set(logging_config.SERVICE_LOG_FILES.values())

    def test_log_file_names_is_ordered_and_immutable(self):
        assert logging_config.LOG_FILE_NAMES == ("api.log", "runner.log", "analysis-worker.log")
        assert isinstance(logging_config.LOG_FILE_NAMES, tuple)


class TestLogFileFor:
    def _settings(self, tmp_path, *, enabled):
        return SimpleNamespace(storage_dir=tmp_path / "storage", log_to_file=enabled)

    def test_returns_none_when_file_logging_is_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logging_config, "settings", self._settings(tmp_path, enabled=False))

        assert logging_config.log_file_for("api") is None

    def test_returns_the_service_path_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logging_config, "settings", self._settings(tmp_path, enabled=True))

        assert logging_config.log_file_for("analysis") == tmp_path / "storage" / "logs" / "analysis-worker.log"

    def test_rejects_an_unknown_service_identity(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logging_config, "settings", self._settings(tmp_path, enabled=True))

        with pytest.raises(ValueError, match="unknown service identity"):
            logging_config.log_file_for("beat")


class TestConfigureLogging:
    def test_default_configuration_writes_only_to_stdout(self, restore_root_handlers):
        logging_config.configure_logging("info")

        handlers = _astinuslab_handlers()
        assert len(handlers) == 1
        assert not isinstance(handlers[0], RotatingFileHandler)

    def test_log_file_adds_a_rotating_handler(self, tmp_path, restore_root_handlers):
        log_file = tmp_path / "logs" / "api.log"

        logging_config.configure_logging("info", log_file=log_file)

        rotating = [h for h in _astinuslab_handlers() if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert log_file.parent.is_dir()

    def test_records_reach_the_file_in_the_shared_format(self, tmp_path, restore_root_handlers):
        log_file = tmp_path / "logs" / "runner.log"
        logging_config.configure_logging("info", log_file=log_file)

        logging.getLogger("astinuslab.test").info("http_request method=GET")

        contents = log_file.read_text(encoding="utf-8")
        assert "[INFO] request_id=- astinuslab.test: http_request method=GET" in contents

    def test_reconfiguring_does_not_duplicate_handlers(self, tmp_path, restore_root_handlers):
        log_file = tmp_path / "logs" / "api.log"

        logging_config.configure_logging("info", log_file=log_file)
        logging_config.configure_logging("info", log_file=log_file)

        assert len(_astinuslab_handlers()) == 2  # one stdout, one rotating file

    def test_reconfiguring_without_a_file_removes_the_previous_file_handler(self, tmp_path, restore_root_handlers):
        logging_config.configure_logging("info", log_file=tmp_path / "logs" / "api.log")
        logging_config.configure_logging("info")

        assert not [h for h in _astinuslab_handlers() if isinstance(h, RotatingFileHandler)]

    def test_foreign_handlers_are_preserved(self, tmp_path, restore_root_handlers):
        foreign = logging.NullHandler()
        foreign.name = "celery-owned"
        logging.getLogger().addHandler(foreign)

        logging_config.configure_logging("info", log_file=tmp_path / "logs" / "api.log")

        assert foreign in logging.getLogger().handlers

    def test_the_file_is_size_bounded_and_keeps_backups(self, tmp_path, restore_root_handlers):
        log_file = tmp_path / "logs" / "api.log"

        logging_config.configure_logging("info", log_file=log_file, max_bytes=512, backup_count=2)

        logger = logging.getLogger("astinuslab.rotation")
        for index in range(200):
            logger.info("filler record %03d padded out to force a rollover", index)

        assert log_file.stat().st_size <= 512 * 2
        assert (log_file.parent / "api.log.1").exists()
        # backup_count=2 caps the series; a third backup must never appear.
        assert not (log_file.parent / "api.log.3").exists()

    def test_a_zero_max_bytes_never_rotates(self, tmp_path, restore_root_handlers):
        """A disabled bound is honoured rather than silently replaced by a default."""
        log_file = tmp_path / "logs" / "api.log"

        logging_config.configure_logging("info", log_file=log_file, max_bytes=0, backup_count=3)

        logger = logging.getLogger("astinuslab.no_rotation")
        for index in range(200):
            logger.info("filler record %03d padded out to force a rollover", index)

        assert not (log_file.parent / "api.log.1").exists()

    def test_an_unwritable_log_file_does_not_stop_stdout_logging(self, tmp_path, restore_root_handlers, caplog):
        blocked = tmp_path / "logs"
        blocked.write_text("not a directory", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            logging_config.configure_logging("info", log_file=blocked / "api.log")

        handlers = _astinuslab_handlers()
        assert len(handlers) == 1
        assert not isinstance(handlers[0], RotatingFileHandler)
        assert "file logging disabled" in caplog.text
