"""Tests for the rotating server log writer."""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from maiocr import logging_utils, paths, settings as settings_mod


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch, tmp_path):
    """Each test gets a fresh config and a temp server.log location."""
    fake_log = tmp_path / "server.log"
    monkeypatch.setattr(paths, "SERVER_LOG", fake_log)
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", tmp_path / "settings.yml")
    settings_mod._CURRENT = None
    yield
    settings_mod._CURRENT = None
    # Drop any handlers we attached
    logger = logging.getLogger("maiocr")
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)


def test_configure_creates_handler(tmp_path):
    logger = logging_utils.configure()
    assert logger.handlers, "configure() did not attach a handler"
    assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_rotate_when_size_exceeded(tmp_path):
    s = settings_mod.get_settings(reload=True)
    s.log_max_bytes = 256
    s.log_backup_count = 3
    settings_mod.save_settings(s)

    logger = logging_utils.configure()
    # Write enough lines to force at least one rotation
    payload = "x" * 200
    for i in range(20):
        logger.info(f"line {i:03d} {payload}")
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass

    backups = sorted(tmp_path.glob("server.log.*"))
    assert backups, "rotation did not produce a backup file"
    # The active log still exists
    assert (tmp_path / "server.log").exists()


def test_prune_old_backups(tmp_path):
    # Seed a server.log and two rotated backups.
    # RotatingFileHandler numbers backups so .1 is the *newest* rotated
    # file (the last rollover renamed the previous .1 to .2).
    log = tmp_path / "server.log"
    log.write_text("active\n")
    (tmp_path / "server.log.1").write_text("newest-rotation\n")
    (tmp_path / "server.log.2").write_text("older-rotation\n")
    (tmp_path / "server.log.3").write_text("oldest-rotation\n")

    s = settings_mod.get_settings(reload=True)
    s.log_max_bytes = 10
    s.log_backup_count = 1
    s.log_retention_days = 0
    settings_mod.save_settings(s)

    # Make all backups "old" so they are eligible for pruning
    past = time.time() - 86400 * 30
    for p in (
        tmp_path / "server.log.1",
        tmp_path / "server.log.2",
        tmp_path / "server.log.3",
    ):
        import os
        os.utime(p, (past, past))

    logging_utils._prune_old_backups(log, backup_count=1, retention_days=1)
    remaining = sorted(p.name for p in tmp_path.glob("server.log*"))
    # We always keep backup_count = 1 (.1, the newest rotated)
    assert "server.log" in remaining
    assert "server.log.1" in remaining
    assert "server.log.2" not in remaining
    assert "server.log.3" not in remaining


def test_logging_honours_level(tmp_path):
    s = settings_mod.get_settings(reload=True)
    s.log_level = "warning"
    settings_mod.save_settings(s)

    logger = logging_utils.configure()
    # Each handler should have WARNING level
    for h in logger.handlers:
        assert h.level == logging.WARNING
