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

from maiocr import client, gpu, i18n, notify, settings as settings_mod


SERVICE = "MaiOCR.service"
SERVICE_FALLBACK = "maiocr.service"
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


def _print(line: str) -> None:
    print(line)


def cmd_start(_args) -> int:
    _systemctl(["start", _service_name()])
    notify.notify(i18n.t("notifications.started"))
    return 0


def cmd_stop(_args) -> int:
    _systemctl(["stop", _service_name()])
    notify.notify(i18n.t("notifications.stopped"))
    return 0


def cmd_restart(_args) -> int:
    _systemctl(["restart", _service_name()])
    notify.notify(i18n.t("notifications.restarted"))
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
}


def main(argv: Optional[List[str]] = None) -> int:
    # Boot the i18n system from the user's settings as early as possible
    # so any error message we print below is already localised.
    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)

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
