"""Logging helpers for the MaiOCR server.

The server runs in the background as a systemd user service. We write
a plain-text log next to the runtime socket and rotate it ourselves
so the log cannot grow without bound.

Rotation policy (defaults, override via ``settings``):

* maximum file size — 1 MiB per log file
* number of rotated backups kept — 3
* total retention window — 7 days (backups older than this are removed)

The active log is *never* deleted while the service is running. Only
``server.log.1`` … ``server.log.N`` are eligible for deletion.
"""
from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from maiocr import paths


_DEFAULTS = {
    "max_bytes": 1 * 1024 * 1024,    # 1 MiB
    "backup_count": 3,                # server.log.1 .. server.log.3
    "retention_days": 7,
}


def _settings():
    # Imported lazily so this module is safe to import before the
    # settings module is initialised.
    from maiocr import settings

    s = settings.get_settings()
    return {
        "max_bytes": int(getattr(s, "log_max_bytes", _DEFAULTS["max_bytes"])) or _DEFAULTS["max_bytes"],
        "backup_count": int(getattr(s, "log_backup_count", _DEFAULTS["backup_count"])) or _DEFAULTS["backup_count"],
        "retention_days": int(getattr(s, "log_retention_days", _DEFAULTS["retention_days"])) or _DEFAULTS["retention_days"],
    }


def _level_value(name: str) -> int:
    name = (name or "info").lower()
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(name, logging.INFO)


def configure() -> logging.Logger:
    """Configure the root logger to write to the server log file.

    Returns the MaiOCR-named logger.
    """
    cfg = _settings()
    s = _settings()  # no-op, keep above for clarity
    from maiocr import settings as settings_mod

    s = settings_mod.get_settings()
    log_path = paths.SERVER_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("maiocr")
    logger.setLevel(_level_value(s.log_level))
    logger.propagate = False

    # Remove any handlers we previously attached (idempotent reconfigure)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=cfg["max_bytes"],
        backupCount=cfg["backup_count"],
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(_level_value(s.log_level))
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # Force the handler to open now (RotatingFileHandler with delay=True
    # opens on first emit otherwise, but we want immediate rotation
    # behaviour on the very first write).
    handler._open()  # type: ignore[attr-defined]
    logger.addHandler(handler)

    # Best-effort: prune backups older than retention_days. The active
    # log file is never touched.
    _prune_old_backups(log_path, cfg["backup_count"], cfg["retention_days"])

    # Also tee to stderr so the journal (``maiocr-server`` /
    # ``maiocr-tray`` under systemd) and an interactive terminal both
    # see the log in real time.  When stderr is a TTY we use a short
    # timestamp; otherwise we keep the full timestamp that systemd
    # already records on its own lines.
    try:
        sh = logging.StreamHandler()
        if os.isatty(2):
            datefmt = "%H:%M:%S"
        else:
            datefmt = "%Y-%m-%d %H:%M:%S"
        sh.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(message)s",
                datefmt=datefmt,
            )
        )
        sh.setLevel(_level_value(s.log_level))
        logger.addHandler(sh)
    except Exception:
        # stderr might be closed in odd environments; never let
        # logging setup itself raise.
        pass

    return logger


def _prune_old_backups(
    log_path: Path, backup_count: int, retention_days: int
) -> None:
    """Delete rotated backup files outside the keep window.

    Only files matching the rotation pattern are considered; the active
    log is never touched.

    Convention: ``.1`` is the **newest** rotated file (the most recent
    rollover). Higher numbers are older.
    """
    cutoff = time.time() - retention_days * 86400
    base = log_path.name
    parent = log_path.parent
    # Sort ascending by index: .1 first, then .2, .3, …
    candidates = sorted(
        (p for p in parent.glob(f"{base}.*") if _rotate_index(p, base) is not None),
        key=lambda p: _rotate_index(p, base),
    )
    # Always keep the first ``backup_count`` entries; the rest are
    # subject to the retention window.
    keep = set(candidates[:backup_count])
    for p in candidates:
        if p in keep:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _rotate_index(path: Path, base: str) -> Optional[int]:
    """Return the rotation index of ``path``, or ``None`` if it is not a rotated file."""
    suffix = path.name[len(base) + 1:]  # everything after "<name>."
    if not suffix.isdigit():
        return None
    return int(suffix)


def get_logger() -> logging.Logger:
    """Return the configured MaiOCR logger, configuring lazily."""
    logger = logging.getLogger("maiocr")
    if not logger.handlers:
        configure()
    return logger
