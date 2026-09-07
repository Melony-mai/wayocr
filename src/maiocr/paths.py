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
