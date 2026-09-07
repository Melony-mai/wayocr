"""Remove old wayocr leftovers and obsolete MaiOCR artefacts.

Targets only artefacts that are clearly related to the previous
``wayocr`` implementation or to a now-stale MaiOCR install:

* ``~/.config/systemd/user/wayocr-server.service`` and its symlink
  in ``default.target.wants/``,
* the old ``/tmp/wayocr.sock`` socket,
* old executable symlinks under ``~/.local/bin/wayocr*`` (only those
  pointing at the legacy ``~/.local/share/uv/tools/wayocr`` tool),
* the old uv tool installation under
  ``~/.local/share/uv/tools/wayocr``.

We deliberately do *not* touch unrelated system services, the user's
own shortcuts, niri configs, etc.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


OLD_SERVICE_NAME = "wayocr-server.service"
OLD_TOOL_DIR = Path.home() / ".local" / "share" / "uv" / "tools" / "wayocr"
OLD_BIN_NAMES = ("wayocr", "wayocr-server", "wayocr-status")
OLD_SOCKETS = [
    Path("/tmp/wayocr.sock"),
    Path("/tmp/wayocr-server.sock"),
]
OLD_UNIT = Path.home() / ".config" / "systemd" / "user" / OLD_SERVICE_NAME
OLD_WANTS = (
    Path.home() / ".config" / "systemd" / "user" / "default.target.wants"
)


def _is_old_wayocr_link(link: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        target = link.resolve()
    except Exception:
        return False
    return str(target).startswith(str(OLD_TOOL_DIR))


def cleanup(dry_run: bool = False) -> int:
    print("MaiOCR: removing obsolete wayocr leftovers")
    print("==========================================")

    if shutil.which("systemctl"):
        try:
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    OLD_SERVICE_NAME,
                ],
                check=False,
            )
        except Exception as exc:
            print(f"  disable {OLD_SERVICE_NAME}: {exc}")

    for sock in OLD_SOCKETS:
        if sock.exists():
            print(f"  removing socket {sock}")
            if not dry_run:
                try:
                    sock.unlink()
                except Exception as exc:
                    print(f"    failed: {exc}")

    if OLD_UNIT.exists():
        print(f"  removing unit file {OLD_UNIT}")
        if not dry_run:
            try:
                OLD_UNIT.unlink()
            except Exception as exc:
                print(f"    failed: {exc}")

    if OLD_WANTS.exists():
        sym = OLD_WANTS / OLD_SERVICE_NAME
        if sym.is_symlink():
            print(f"  removing wants symlink {sym}")
            if not dry_run:
                try:
                    sym.unlink()
                except Exception as exc:
                    print(f"    failed: {exc}")

    for name in OLD_BIN_NAMES:
        link = Path.home() / ".local" / "bin" / name
        if not (link.exists() or link.is_symlink()):
            continue
        if not _is_old_wayocr_link(link):
            print(f"  keeping {link} (not the legacy wayocr tool)")
            continue
        print(f"  removing symlink {link}")
        if not dry_run:
            try:
                link.unlink()
            except Exception as exc:
                print(f"    failed: {exc}")

    if OLD_TOOL_DIR.exists():
        print(f"  removing old tool install {OLD_TOOL_DIR}")
        if not dry_run:
            try:
                shutil.rmtree(OLD_TOOL_DIR)
            except Exception as exc:
                print(f"    failed: {exc}")

    if shutil.which("systemctl"):
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], check=False
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed"], check=False
            )
        except Exception as exc:
            print(f"  daemon-reload: {exc}")

    print("done.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(cleanup(dry_run="--dry-run" in sys.argv))
