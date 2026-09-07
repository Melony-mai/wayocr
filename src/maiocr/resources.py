"""Resource discovery for MaiOCR.

Locates the application icon (and any other non-Python asset) whether
MaiOCR is:

* running from a source checkout (project root),
* running from an installed ``uv tool`` environment (tool's bin
  directory),
* running from a system ``pip install`` (site-packages).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional


APP_NAME = "maiocr"


def _candidate_roots() -> Iterable[Path]:
    """Yield plausible MaiOCR data roots in priority order."""
    # 0. Inside the installed python package (always works for both
    #    ``uv tool install`` and ``pip install``).
    yield Path(__file__).resolve().parent / "data"

    # 1. XDG_DATA_HOME/maiocr (managed by install script)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        yield Path(xdg) / APP_NAME
    yield Path.home() / ".local" / "share" / APP_NAME

    # 2. /usr/local/share and /usr/share
    for p in ("/usr/local/share", "/usr/share"):
        yield Path(p) / APP_NAME

    # 3. Inside the source tree (dev mode): the project root.
    yield Path(__file__).resolve().parents[2]


def find_icon(name: str = "icon.ico") -> Optional[Path]:
    """Return the first existing icon path or None."""
    for root in _candidate_roots():
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def find_resource(*parts: str) -> Optional[Path]:
    for root in _candidate_roots():
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return candidate
    return None


def install_icon(source: Path, dest_dir: Path) -> Path:
    """Copy ``source`` into ``dest_dir`` and return the installed path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / source.name
    shutil.copy2(source, target)
    os.chmod(target, 0o644)
    return target
