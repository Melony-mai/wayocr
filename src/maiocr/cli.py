"""`maiocr` command: manage the service, trigger OCR, edit settings.

Subcommands:

* ``maiocr``                  -> take a screenshot, OCR, copy to clipboard
* ``maiocr run``              -> alias of the above
* ``maiocr start``            -> systemctl --user start MaiOCR.service
* ``maiocr stop``             -> systemctl --user stop MaiOCR.service
* ``maiocr restart``          -> systemctl --user restart MaiOCR.service
* ``maiocr enable``           -> systemctl --user enable MaiOCR.service
* ``maiocr disable``          -> systemctl --user disable MaiOCR.service
* ``maiocr status``           -> full status (systemd, socket, engine)
* ``maiocr release-vram``     -> tell the server to drop the OCR engine
* ``maiocr gpu-info``         -> print the detected GPU provider
* ``maiocr ping``             -> verify the server is reachable
* ``maiocr logs [-f]``        -> journalctl --user -u MaiOCR
* ``maiocr tray``             -> start the system-tray indicator
* ``maiocr settings show``    -> print current settings
* ``maiocr settings set ...`` -> change a setting
* ``maiocr cleanup``          -> remove obsolete wayocr leftovers
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from maiocr import client, gpu, i18n, notifications, settings as settings_mod
# notify is the legacy shim; prefer the new ``notifications`` module
# for high-level helpers (notify_service_started, notify_ocr_completed,
# …). ``notify.notify`` still works for one-off messages.
from maiocr import notify  # noqa: E402


SERVICE = "MaiOCR.service"
SERVICE_FALLBACK = "maiocr.service"
TRAY_SERVICE = "MaiOCR-tray.service"
TRAY_SERVICE_FALLBACK = "maiocr-tray.service"
OLD_SERVICE = "wayocr-server.service"


def _systemctl(args, check=True):
    """Run a systemctl command against the active service name."""
    last_error: Optional[subprocess.CalledProcessError] = None
    for name in (SERVICE, SERVICE_FALLBACK):
        try:
            return subprocess.run(
                ["systemctl", "--user", *args],
                check=check,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if name == SERVICE_FALLBACK:
                raise
            continue
    if last_error:
        raise last_error
    return None


def _service_name() -> str:
    return SERVICE if Path.home().joinpath(
        ".config/systemd/user", SERVICE
    ).exists() else SERVICE_FALLBACK


def _tray_service_name() -> str:
    return TRAY_SERVICE if Path.home().joinpath(
        ".config/systemd/user", TRAY_SERVICE
    ).exists() else TRAY_SERVICE_FALLBACK


def _tray_unit_installed() -> bool:
    """Return True if the tray unit file is present in the user
    systemd directory. The tray is a separate process and is
    optional — we only start/restart it when its unit file exists.
    """
    home = Path.home() / ".config/systemd/user"
    return (home / TRAY_SERVICE).exists() or (home / TRAY_SERVICE_FALLBACK).exists()


def _print(line: str) -> None:
    print(line)


def cmd_start(_args) -> int:
    # Start the server first so the tray can connect to it.
    _systemctl(["start", _service_name()])
    # Always restart the tray. ``systemctl start`` is a no-op when
    # the unit is already active, but we want the tray to pick up
    # any binary that was updated since the last start. The tray's
    # self-check (see ``maiocr.tray._binary_stale``) also forces
    # systemd to start a fresh process on the new interpreter.
    if _tray_unit_installed():
        subprocess.run(
            ["systemctl", "--user", "restart", _tray_service_name()],
            check=False,
        )
    # Also drop a notification request in case the tray process is
    # not running (custom mode only — the SystemBackend would
    # handle it itself if the user picked system mode).
    if settings_mod.get_settings().notification_mode == "custom":
        _request_tray_bubble(
            "service.started.request",
            notifications.EventKind.SUCCESS,
            "notifications.title.service",
            "notifications.started",
        )
    else:
        notifications.notify_service_started()
    return 0


def cmd_stop(_args) -> int:
    _systemctl(["stop", _service_name()])
    # Stop the tray too so the user gets a clean shutdown when they
    # run ``maiocr stop``. The systemd unit's ``PartOf=`` used to
    # do this automatically; we removed that so the tray could
    # survive a brief server outage, but on a full stop we want
    # the tray gone too.
    if _tray_unit_installed():
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", _tray_service_name()],
                check=False,
            )
        except Exception:
            pass
    if settings_mod.get_settings().notification_mode == "custom":
        _request_tray_bubble(
            "service.stopped.request",
            notifications.EventKind.INFO,
            "notifications.title.service",
            "notifications.stopped",
        )
    else:
        notifications.notify_service_stopped()
    return 0


def cmd_restart(_args) -> int:
    """Restart the server, and restart the tray to pick up any
    new binary that was installed in the meantime.
    """
    _systemctl(["restart", _service_name()])
    if _tray_unit_installed():
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", _tray_service_name()],
                check=False,
            )
        except Exception:
            pass
    if settings_mod.get_settings().notification_mode == "custom":
        _request_tray_bubble(
            "service.restarted.request",
            notifications.EventKind.INFO,
            "notifications.title.service",
            "notifications.restarted",
        )
    else:
        notifications.notify_service_restarted()
    return 0


def _request_tray_bubble(
    event_id: str,
    kind: "notifications.EventKind",
    title_key: str,
    body_key: str,
) -> None:
    """Drop a notification request that the tray process will
    pick up on its next refresh.  Used in custom mode from CLI
    commands that have no Qt event loop.
    """
    from maiocr import i18n
    from maiocr.notifications import Event, send_request
    try:
        send_request(
            Event(
                id=event_id,
                kind=kind,
                title=i18n.t(title_key),
                body=i18n.t(body_key),
            )
        )
    except Exception:
        pass


def cmd_refresh_tray(_args) -> int:
    """Restart the tray icon.  Useful after a manual ``uv tool
    install`` that did not go through the install script: the tray
    is left holding an old binary, and this command picks up the
    new one.
    """
    if not _tray_unit_installed():
        print("tray unit not installed")
        return 1
    name = _tray_service_name()
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", name], check=True
        )
    except subprocess.CalledProcessError as exc:
        print(f"failed to restart {name}: {exc}", file=sys.stderr)
        return 1
    print(f"restarted {name}")
    return 0


def cmd_repair(_args) -> int:
    """Re-link system PyQt6 into the maiocr tool venv and restart
    the tray.  This is the CLI counterpart of the install
    script's PyQt6 linking step.  Run it once after manually
    reinstalling the tool if the tray falls back to headless
    mode.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    system_py = _shutil.which("python3") or _shutil.which("python")
    if not system_py:
        print("no system python3 found", file=sys.stderr)
        return 1
    # Discover the system Python's site-packages.
    proc = subprocess.run(
        [system_py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print("could not discover system site-packages", file=sys.stderr)
        return 1
    sys_site = _Path(proc.stdout.strip())
    if not (sys_site / "PyQt6").is_dir():
        print(
            f"PyQt6 not found in {sys_site}. Install python-pyqt6 "
            f"first: sudo pacman -S python-pyqt6",
            file=sys.stderr,
        )
        return 1
    # Find the tool's venv site-packages.
    tool_py = _Path.home() / ".local/share/uv/tools/maiocr/bin/python"
    if not tool_py.is_file():
        print(f"tool python not found at {tool_py}", file=sys.stderr)
        return 1
    # Determine the tool venv's Python version by asking it.
    proc = subprocess.run(
        [str(tool_py), "-c", "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print("could not determine tool python version", file=sys.stderr)
        return 1
    py_ver = proc.stdout.strip()
    site_base = _Path(
        f"{_Path.home()}/.local/share/uv/tools/maiocr/lib/python{py_ver}/site-packages"
    )
    if not site_base.is_dir():
        print(
            f"tool venv site-packages not found at {site_base}",
            file=sys.stderr,
        )
        return 1
    # Link every PyQt6* directory.
    linked = 0
    for pkg_dir in sys_site.glob("PyQt6*"):
        if not pkg_dir.is_dir():
            continue
        name = pkg_dir.name
        target = site_base / name
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(pkg_dir)
        linked += 1
    for info in sys_site.glob("PyQt6*.dist-info"):
        if not info.is_dir():
            continue
        target = site_base / info.name
        if target.exists():
            if target.is_dir():
                _shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
        _shutil.copytree(str(info), str(target))
        linked += 1
    # Verify.
    try:
        subprocess.run(
            [str(tool_py), "-c", "import PyQt6.QtWidgets"],
            check=True, capture_output=True,
        )
        print("PyQt6 available in the maiocr tool venv")
    except subprocess.CalledProcessError:
        print("PyQt6 still unavailable; check the install", file=sys.stderr)
        return 1
    # Restart the tray to pick up the new PyQt6.
    if _tray_unit_installed():
        subprocess.run(
            ["systemctl", "--user", "restart", _tray_service_name()],
            check=False,
        )
    return 0


def cmd_enable(_args) -> int:
    _systemctl(["enable", _service_name()])
    return 0


def cmd_disable(_args) -> int:
    _systemctl(["disable", _service_name()])
    return 0


def cmd_status(_args) -> int:
    return subprocess.call([sys.executable, "-m", "maiocr.status"])


def cmd_release_vram(_args) -> int:
    if not client.is_running(timeout=0.5):
        print(i18n.t("errors.server_unreachable"))
        return 1
    try:
        resp = client.release_vram(timeout=10.0)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        print(i18n.t("errors.server_unreachable"))
        print(f"  ({exc.__class__.__name__}: {exc})")
        return 1
    if resp is None:
        print(i18n.t("errors.server_unreachable"))
        return 1
    if resp.get("error"):
        # client.release_vram swallows socket errors into the
        # ``error`` field. Distinguish "server unreachable" from real
        # server-side errors.
        err = resp["error"]
        if (
            "No such file" in err
            or "Connection refused" in err
            or "Errno 111" in err
            or "Errno 2" in err
        ):
            print(i18n.t("errors.server_unreachable"))
            print(f"  ({err})")
        else:
            print(f"error: {err}")
        return 1
    if resp.get("released"):
        print(i18n.t("notifications.vram_released"))
    else:
        print(i18n.t("notifications.vram_already_unloaded"))
    return 0


def cmd_ping(_args) -> int:
    if client.is_running():
        print("pong")
        return 0
    print(i18n.t("errors.server_unreachable"))
    return 1


def cmd_gpu_info(_args) -> int:
    info = gpu.detect()
    print(f"provider:         {info.provider}")
    print(f"GPU available:    {info.available}")
    print(f"device:           {info.device_name or '(none)'}")
    print(f"reason:           {info.reason}")
    return 0


def cmd_logs(args) -> int:
    cmd = ["journalctl", "--user", "-u", _service_name()]
    if args.follow:
        cmd.append("-f")
    if args.lines:
        cmd.extend(["-n", str(args.lines)])
    return subprocess.call(cmd)


def cmd_run(_args) -> int:
    from maiocr.main import main as run_main
    return run_main()


def cmd_tray(_args) -> int:
    try:
        from maiocr.tray import main as tray_main
    except ImportError as exc:
        print(f"tray unavailable: {exc}")
        return 1
    return tray_main()


def cmd_cleanup(args) -> int:
    from maiocr.cleanup import cleanup as do_cleanup

    return do_cleanup(dry_run=args.dry_run)


def cmd_clean_logs(_args) -> int:
    """Rotate and prune the server log immediately."""
    from maiocr.logging_utils import configure, _prune_old_backups
    from maiocr import paths

    configure()
    # Force a small rotation by appending a marker then rolling the
    # handler's emit() a few times.
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger("maiocr")
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler):
            h.doRollover()
            break
    _prune_old_backups(
        paths.SERVER_LOG,
        backup_count=root.handlers[0].backupCount if root.handlers else 3,
        retention_days=0,  # delete everything but the keep window
    )
    print("logs rotated")
    return 0


# --- settings ---------------------------------------------------------


def cmd_settings_show(_args) -> int:
    s = settings_mod.get_settings()
    print(json.dumps(s.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_settings_set(args) -> int:
    s = settings_mod.get_settings()
    updates: dict = {}
    for pair in args.key_value:
        if "=" not in pair:
            print(f"invalid setting (expected key=value): {pair}", file=sys.stderr)
            return 2
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "language":
            updates[k] = i18n.resolve_language(v)
        elif k in ("auto_release_vram", "prefer_gpu", "notify_on_copy"):
            updates[k] = v.lower() in ("1", "true", "yes", "on")
        elif k == "auto_release_seconds":
            updates[k] = int(v)
        elif k == "log_level":
            if v not in ("debug", "info", "warning", "error"):
                print(f"invalid log_level: {v}", file=sys.stderr)
                return 2
            updates[k] = v
        elif k == "log_max_bytes":
            updates[k] = max(64 * 1024, int(v))
        elif k in ("log_backup_count", "log_retention_days"):
            updates[k] = max(0, int(v))
        elif k == "notification_mode":
            if v not in ("system", "dbus", "custom"):
                print(
                    "invalid notification_mode: "
                    f"{v} (use 'system', 'dbus' or 'custom')",
                    file=sys.stderr,
                )
                return 2
            # ``custom`` is accepted as a synonym for ``dbus`` so old
            # settings files keep working.  We normalise to ``dbus``
            # on the way in.
            updates[k] = "dbus" if v == "custom" else v
        elif k == "notification_duration":
            updates[k] = max(1, int(v))
        elif k in ("click_action_left", "click_action_double"):
            from maiocr import tray_actions
            normalised = tray_actions.normalize(v, default="none")
            if normalised != v and v != "none":
                # The user provided an unknown id; warn but accept
                # the normalised value so the next save round-trips
                # the file.
                print(
                    f"warning: unknown click action {v!r}; "
                    f"falling back to {normalised!r}",
                    file=sys.stderr,
                )
            updates[k] = normalised
        else:
            print(f"unknown setting: {k}", file=sys.stderr)
            return 2
    s.update(**updates)
    settings_mod.save_settings(s)
    i18n.set_default_language(s.language)
    # Push to the running server so it picks the new language immediately
    # (only language is sent over the wire for now; other settings take
    # effect on the next service start).
    if "language" in updates:
        client.set_remote_settings({"language": s.language})
    print(json.dumps(s.as_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_settings_lang(args) -> int:
    """Convenience: ``maiocr settings lang zh-CN``."""
    s = settings_mod.get_settings()
    s.language = i18n.resolve_language(args.language)
    settings_mod.save_settings(s)
    i18n.set_default_language(s.language)
    client.set_remote_settings({"language": s.language})
    print(f"language -> {s.language}")
    return 0


# --- parser -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maiocr", description="MaiOCR management CLI")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("run", help="capture a screenshot and OCR it (default)")
    sub.add_parser("start", help="start the MaiOCR systemd user service")
    sub.add_parser("stop", help="stop the MaiOCR systemd user service")
    sub.add_parser("restart", help="restart the MaiOCR systemd user service")
    sub.add_parser("enable", help="enable MaiOCR at login")
    sub.add_parser("disable", help="disable MaiOCR at login")
    sub.add_parser("status", help="print detailed service status")
    sub.add_parser("release-vram", help="drop the OCR model and free GPU memory")
    sub.add_parser("ping", help="check whether the server is reachable")
    sub.add_parser("gpu-info", help="show detected GPU execution provider")
    sub.add_parser("tray", help="start the system-tray status indicator")

    p_logs = sub.add_parser("logs", help="show service logs")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.add_argument("-n", "--lines", type=int, default=80)

    p_clean = sub.add_parser("cleanup", help="remove obsolete wayocr leftovers")
    p_clean.add_argument("--dry-run", action="store_true")

    sub.add_parser("clean-logs", help="rotate the server log immediately")
    sub.add_parser(
        "refresh-tray",
        help="restart the tray icon (picks up a freshly-installed binary)",
    )
    sub.add_parser(
        "repair",
        help="re-link system PyQt6 into the tool venv and restart the tray",
    )

    # `settings` is handled in main() via a separate parser so that
    # nested subcommands are not mixed with the top-level ones.
    sub.add_parser("settings", help="view or change MaiOCR settings")

    return p


DISPATCH = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "status": cmd_status,
    "release-vram": cmd_release_vram,
    "ping": cmd_ping,
    "gpu-info": cmd_gpu_info,
    "logs": cmd_logs,
    "run": cmd_run,
    "tray": cmd_tray,
    "cleanup": cmd_cleanup,
    "clean-logs": cmd_clean_logs,
    "refresh-tray": cmd_refresh_tray,
    "repair": cmd_repair,
}


def main(argv: Optional[List[str]] = None) -> int:
    # Boot the i18n system from the user's settings as early as possible
    # so any error message we print below is already localised.
    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)
    # Initialise the notification manager so that ``start`` /
    # ``stop`` / ``restart`` show the right notification for the
    # current ``notification_mode`` setting.
    notifications.initialise()

    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["run"]

    if argv and argv[0] == "settings":
        settings_parser = build_settings_parser()
        if len(argv) < 2:
            settings_parser.print_help()
            return 2
        sub_args = settings_parser.parse_args(argv[1:])
        cmd = sub_args.settings_command
        if cmd == "show" or cmd is None:
            return cmd_settings_show(sub_args)
        if cmd == "set":
            return cmd_settings_set(sub_args)
        if cmd == "lang":
            return cmd_settings_lang(sub_args)
        settings_parser.print_help()
        return 2

    args = parser.parse_args(argv)
    handler = DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


def build_settings_parser() -> argparse.ArgumentParser:
    """Separate parser for ``maiocr settings ...`` so the sub-sub-commands
    are parsed without confusing the top-level one."""
    p = argparse.ArgumentParser(prog="maiocr settings", description="MaiOCR settings")
    sub = p.add_subparsers(dest="settings_command")
    sub.add_parser("show", help="print current settings")
    p_set = sub.add_parser("set", help="set one or more settings")
    p_set.add_argument(
        "key_value",
        nargs="+",
        help="settings in key=value form "
        "(language, auto_release_vram, prefer_gpu, notify_on_copy, "
        "auto_release_seconds, log_level)",
    )
    p_lang = sub.add_parser("lang", help="set UI language")
    p_lang.add_argument("language", help="language code (en, zh-CN)")
    return p


if __name__ == "__main__":
    sys.exit(main())
