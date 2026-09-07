"""Runtime configuration for MaiOCR.

Resolves where sockets, status files, and runtime state live. Honors
XDG_RUNTIME_DIR when present (systemd user services always set it),
and falls back to a per-user tmp dir otherwise.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP_NAME = "maiocr"


def _runtime_root() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}"


RUNTIME_DIR: Path = _runtime_root()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

SOCKET_PATH: Path = RUNTIME_DIR / "maiocr.sock"
STATUS_FILE: Path = RUNTIME_DIR / "status.json"
PID_FILE: Path = RUNTIME_DIR / "maiocr.pid"
SERVER_LOG: Path = RUNTIME_DIR / "server.log"
# Notification requests are written by the CLI / main process
# and read by the tray.  When the tray polls this directory and
# finds a new ``*.json`` file, it shows a notification bubble and
# deletes the file.  This is the IPC channel that lets ``maiocr``
# running from a terminal cause a bubble in the tray.
NOTIFY_DIR: Path = RUNTIME_DIR / "notifications"
NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
