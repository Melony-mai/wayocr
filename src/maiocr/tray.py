"""Niri / DankMaterialShell status-bar integration.

This module provides a self-contained background process that:

* keeps a system-tray (SNI) icon visible while MaiOCR is running,
* polls the MaiOCR server and writes ``$XDG_RUNTIME_DIR/maiocr/
  status.json`` so DMS Quickshell widgets can render the same status,
* shows a context menu with the actions required by the project's
  spec (Release VRAM, Restart MaiOCR) and a few extras,
* offers a Preferences dialog that edits ``settings.yml`` live and
  pushes the new language to the running server,
* opens a real GUI dialog for **Service Status** and **GPU
  Information** instead of spawning a child process that prints to
  stdout (which would be invisible when running as a daemon),
* works whether the GUI toolkit is available or not: when PyQt6 is
  missing it just refreshes the status file in a background loop.

The icon is loaded from the path returned by
:func:`maiocr.resources.find_icon`, so it works after installation on
any machine.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("maiocr.tray")

from maiocr import client, gpu, i18n, notifications, resources, settings as settings_mod
from maiocr import paths, system_info, tray_actions


SERVICE = "MaiOCR.service"


def _drain_notification_requests() -> int:
    """Read every ``*.json`` in ``NOTIFY_DIR``, dispatch each as a
    notification through the manager, and delete the file.

    Returns the number of requests processed.  Called from the
    tray's ``refresh()`` so the bubble appears within a couple of
    seconds of the CLI writing the request.
    """
    import json
    import os

    notify_dir = paths.NOTIFY_DIR
    if not notify_dir.is_dir():
        return 0
    # Sort by filename (timestamp prefix) so older requests are
    # processed first.
    files = sorted(notify_dir.glob("*.json"))
    count = 0
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            ev = notifications.Event(
                id=str(data.get("id", "request")),
                kind=notifications.EventKind(str(data.get("kind", "info"))),
                title=str(data.get("title", "")),
                body=str(data.get("body", "")),
            )
            # Route through the manager so the active backend
            # (custom bubble or system) handles it.  The manager
            # already has a mutex so this is safe.
            notifications.get_manager().notify_event(ev)
            count += 1
        except Exception as exc:
            log.warning("failed to handle notify request %s: %s", path, exc)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    # Also clean up any leftover ``*.tmp`` files from a previous
    # crash mid-write.
    for path in notify_dir.glob("*.tmp"):
        try:
            os.unlink(path)
        except OSError:
            pass
    return count


def _binary_stale() -> bool:
    """Return True if the running Python interpreter or the
    ``maiocr-tray`` entry point was modified after this process
    started.  The tray uses this to detect a stale binary left over
    from a previous install and exit gracefully so systemd can
    restart it on the new interpreter.
    """
    try:
        import os
        my_pid = os.getpid()
        # ``/proc/<pid>/stat`` field 22 (1-indexed) is the start
        # time in clock ticks since boot.  Fall back to /proc
        # ``/status`` ``Birth`` line for older kernels.
        my_start_seconds: float
        try:
            with open(f"/proc/{my_pid}/stat") as fh:
                fields = fh.read().split()
            # 22nd field is start time in clock ticks since boot.
            start_ticks = int(fields[21])
            clk = os.sysconf("SC_CLK_TCK")
            if clk <= 0:
                clk = 100
            my_start_seconds = start_ticks / clk
        except Exception:
            # Fallback: /proc/<pid>/status ``Birth`` line.
            with open(f"/proc/{my_pid}/status") as fh:
                for line in fh:
                    if line.startswith("Birth:"):
                        start_jiffies = int(line.split()[1])
                        clk = os.sysconf("SC_CLK_TCK") or 100
                        my_start_seconds = start_jiffies / clk
                        break
                else:
                    return False

        # Compare against the venv's python binary mtime.
        for candidate in (
            sys.executable,
            os.path.realpath(sys.executable),
        ):
            try:
                mtime = os.path.getmtime(candidate)
                if mtime > my_start_seconds + 1.0:
                    return True
            except OSError:
                continue

        # Compare against the maiocr-tray entry point that systemd
        # invokes.
        tray_bin = Path.home() / ".local/bin/maiocr-tray"
        try:
            if tray_bin.exists() and tray_bin.stat().st_mtime > my_start_seconds + 1.0:
                return True
        except OSError:
            pass
        return False
    except Exception:
        return False


def _status_path() -> Path:
    return paths.STATUS_FILE


def _ensure_status_file() -> None:
    status = _status_path()
    if status.exists():
        return
    info = gpu.detect()
    status.write_text(
        json.dumps(
            {
                "service": "maiocr",
                "engine_loaded": False,
                "provider": "unloaded",
                "gpu_available": info.available,
                "gpu_device": info.device_name,
                "updated": time.time(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def _refresh_status() -> dict:
    info = client.status(timeout=1.0) or {}
    gpu_info = gpu.detect()
    payload = {
        "service": "maiocr",
        "engine_loaded": bool(info.get("engine_loaded", False)),
        "provider": info.get("provider", "unloaded"),
        "gpu_available": gpu_info.available,
        "gpu_device": gpu_info.device_name,
        "updated": time.time(),
    }
    try:
        _status_path().write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )
    except Exception:
        pass
    return payload


# ---------------------------------------------------------------------------
# CLI subcommands used by the tray menu items.
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "maiocr.cli", *args])


# ---------------------------------------------------------------------------
# Plain-text formatters used by the dialogs. Defined at module level so
# they can be exercised by tests and by headless callers without
# requiring PyQt6.
# ---------------------------------------------------------------------------


def format_status_text() -> str:
    s = settings_mod.get_settings()
    data = system_info.service_status()
    lines = [
        i18n.t("app.name") + " — " + i18n.t("actions.service_status"),
        "",
    ]
    svc = data.get("service", {})
    lines.append(
        f"{i18n.t('status.service_label')}: {svc.get('systemctl', 'unknown')}"
    )
    if svc.get("enabled") is not None:
        lines.append(f"systemd enabled: {svc['enabled']}")
    if svc.get("since"):
        lines.append(f"active since:     {svc['since']}")
    if data.get("pid"):
        lines.append(f"PID:              {data['pid']}")
    lines.append(
        f"socket:           {data.get('socket', 'unknown')} "
        f"({data.get('socket_path', '')})"
    )
    if data.get("ping") == "ok":
        srv = data.get("server", {})
        engine_loaded = srv.get("engine_loaded", False)
        provider = srv.get("provider", "unloaded")
        if engine_loaded:
            lines.append(i18n.t("status.engine_loaded") + f" ({provider})")
        else:
            lines.append(i18n.t("status.engine_unloaded"))
    else:
        lines.append(f"ping:             {data.get('ping')}")
    gpu_info = data.get("runtime", {})
    if gpu_info.get("exists"):
        lines.append(
            i18n.t("status.gpu_available")
            if gpu_info.get("gpu_available")
            else i18n.t("status.gpu_unavailable")
        )
        if gpu_info.get("gpu_device"):
            lines.append(
                i18n.t("status.gpu_device", device=gpu_info["gpu_device"])
            )
    lines.append("")
    lines.append(f"auto_release_vram: {s.auto_release_vram}")
    lines.append(f"auto_release_seconds: {s.auto_release_seconds}")
    lines.append(f"log_level:        {s.log_level}")
    return "\n".join(lines)


def format_gpu_text() -> str:
    info = system_info.gpu_runtime()
    lines = [i18n.t("app.name") + " — " + i18n.t("actions.gpu_info"), ""]
    if info["available"]:
        lines.append(i18n.t("status.gpu_available"))
        if info["device_name"]:
            lines.append(
                i18n.t("status.gpu_device", device=info["device_name"])
            )
        lines.append(f"execution provider: {info['provider']}")
        lines.append(f"reason:           {info['provider_reason']}")
    else:
        lines.append(i18n.t("status.gpu_unavailable"))
        lines.append(f"provider:         {info['provider']}")
        lines.append(f"reason:           {info['provider_reason']}")
    lines.append("")
    lines.append(
        f"ONNX providers:   {', '.join(info['onnx_providers']) or '(unknown)'}"
    )
    lines.append("")
    if info["vram_total_mb"] is not None:
        used = info["vram_used_mb"] or 0
        total = info["vram_total_mb"]
        pct = (used / total * 100.0) if total else 0.0
        lines.append(
            f"VRAM:             {used} / {total} MiB ({pct:.1f}%)"
        )
    else:
        lines.append("VRAM:             (nvidia-smi not available)")
    if info["gpu_util_pct"] is not None:
        lines.append(f"GPU util:         {info['gpu_util_pct']}%")
    if info["temperature_c"] is not None:
        lines.append(f"Temperature:      {info['temperature_c']} °C")
    if info["power_w"] is not None:
        lines.append(f"Power draw:       {info['power_w']} W")
    lines.append("")
    engine_provider = info["engine_provider"] or "unloaded"
    if info["engine_using_gpu"]:
        lines.append(f"OCR engine:       loaded on GPU ({engine_provider})")
    else:
        lines.append(f"OCR engine:       {engine_provider}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Menu callbacks. Exposed at module level so they work even if PyQt6 is
# not available (the headless loop can still call them when needed).
# ---------------------------------------------------------------------------


def release_vram() -> None:
    """Send ``release_vram`` to the running server and notify the user."""
    if not client.is_running(timeout=0.5):
        notifications.notify_error(i18n.t("errors.server_unreachable"))
        return
    try:
        resp = client.release_vram(timeout=10.0)
    except Exception as exc:
        notifications.notify_error(f"{exc}")
        return
    if resp is None or resp.get("error"):
        notifications.notify_error(
            str(resp.get("error") if resp else "no response")
        )
    elif resp.get("released"):
        # The tray process IS the event loop, so we can call the
        # notification backend directly.
        notifications.notify_vram_released()
    else:
        notifications.notify_vram_already_unloaded()


def restart_service() -> None:
    notifications.notify_service_restarted()
    _run_cli("restart")
    time.sleep(0.7)


def start_service() -> None:
    _run_cli("start")
    time.sleep(0.7)
    if client.is_running(timeout=1.0):
        notifications.notify_service_started()


def stop_service() -> None:
    _run_cli("stop")
    time.sleep(0.7)
    if not client.is_running(timeout=1.0):
        notifications.notify_service_stopped()


def run_ocr() -> None:
    """Spawn the capture-and-OCR client as a separate process."""
    try:
        subprocess.Popen([sys.executable, "-m", "maiocr.cli", "run"])
    except Exception as exc:
        notifications.notify_error(f"{exc}")


def show_status() -> str:
    """Return the Service Status text and (if not in headless mode)
    also pop up a GUI dialog.

    The text is always returned so headless callers (tests, CLI) can
    print it. When PyQt6 is loaded we additionally open a dialog.
    """
    body = format_status_text()
    _try_popup(i18n.t("actions.service_status"), body)
    return body


def show_gpu_info() -> str:
    body = format_gpu_text()
    _try_popup(i18n.t("actions.gpu_info"), body)
    return body


def _try_popup(title: str, body: str) -> None:
    """Open a PyQt6 dialog if a display is available; otherwise noop."""
    try:
        from PyQt6.QtCore import QSize, Qt  # type: ignore  # noqa: F401
        from PyQt6.QtGui import QFont, QIcon  # type: ignore  # noqa: F401
        from PyQt6.QtWidgets import (  # type: ignore
            QApplication,
            QDialog,
            QDialogButtonBox,
            QPlainTextEdit,
            QVBoxLayout,
        )
    except Exception:
        return

    if QApplication.instance() is None:
        return  # no display / no event loop

    app = QApplication.instance()
    dlg = QDialog()
    dlg.setWindowTitle(title)
    dlg.resize(560, 420)
    layout = QVBoxLayout(dlg)
    view = QPlainTextEdit(dlg)
    view.setReadOnly(True)
    font = QFont("Monospace")
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    view.setFont(font)
    view.setPlainText(body)
    layout.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dlg.accept)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    dlg.exec()


# ---------------------------------------------------------------------------
# PyQt6 GUI implementation.
# ---------------------------------------------------------------------------


def _try_qt_tray() -> int | None:
    try:
        from PyQt6.QtCore import QSize, Qt, QTimer  # type: ignore
        from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap  # type: ignore
        from PyQt6.QtWidgets import (  # type: ignore
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QMenu,
            QPlainTextEdit,
            QPushButton,
            QSpinBox,
            QSystemTrayIcon,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:  # pragma: no cover - depends on runtime env
        print(f"PyQt6 unavailable: {exc}", file=sys.stderr)
        return None

    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(i18n.t("app.name"))

    icon_path = resources.find_icon("icon.ico")
    app_icon: QIcon
    if icon_path is not None:
        app_icon = QIcon(str(icon_path))
    else:
        pix = QPixmap(64, 64)
        pix.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(80, 140, 220))
        painter.setPen(QColor(20, 20, 20))
        painter.drawEllipse(8, 8, 48, 48)
        painter.setPen(QColor(255, 255, 255))
        f = painter.font()
        f.setBold(True)
        f.setPointSize(22)
        painter.setFont(f)
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "M")
        painter.end()
        app_icon = QIcon(pix)

    app.setWindowIcon(app_icon)

    def make_overlay_icon(base: QIcon, state: str) -> QIcon:
        """Overlay a coloured dot on the icon to reflect state."""
        pix = base.pixmap(QSize(64, 64))
        if pix.isNull():
            return base
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if state == "gpu":
            color = QColor(80, 200, 120)
        elif state == "cpu":
            color = QColor(80, 140, 220)
        elif state == "off":
            color = QColor(160, 80, 80)
        else:
            color = QColor(180, 180, 60)
        painter.setBrush(color)
        painter.setPen(QColor(20, 20, 20))
        painter.drawEllipse(40, 40, 20, 20)
        painter.end()
        return QIcon(pix)

    def _is_service_running() -> bool:
        """Return True if the MaiOCR server is reachable on the
        Unix socket.  This is the right signal for the tray
        because ``engine_loaded`` is False for the entire warm-up
        period after ``maiocr start`` — the server is up but the
        model has not been loaded yet — and we do not want to
        confuse that with "service is not running".
        """
        try:
            return client.is_running(timeout=0.5)
        except Exception:
            return False

    def state_from_payload(payload: dict) -> str:
        """The tray's state machine has three values:

        * ``"off"``   — the MaiOCR server is not running.
        * ``"idle"``  — the server is up but no engine is loaded.
        * ``"gpu"`` / ``"cpu"`` — the engine is loaded.

        Transitions ``off`` ↔ ``idle`` (and ``off`` ↔ ``gpu``) are
        the ones the user cares about for notifications: they mark
        the moment the service actually went up or down.
        """
        engine_loaded = bool(payload.get("engine_loaded", False))
        provider = payload.get("provider", "unloaded")
        if not engine_loaded:
            # The status file always shows ``engine_loaded: false``
            # for the "service is up but model is unloaded" case.
            # We disambiguate against the actual socket to decide
            # whether the service is alive at all.
            if not _is_service_running():
                return "off"
            # Distinguish: did the server *just* come up (we
            # observed the service starting this iteration) or has
            # it been up for a while?  ``provider == "unloaded"``
            # is the canonical "server up, no model" state.  We
            # report it as ``"idle"``.
            if provider in ("unloaded", ""):
                return "idle"
            return "gpu" if provider in (
                "CUDAExecutionProvider", "TensorrtExecutionProvider"
            ) else "cpu"
        if provider in ("CUDAExecutionProvider", "TensorrtExecutionProvider"):
            return "gpu"
        if provider == "CPUExecutionProvider":
            return "cpu"
        return "idle"

    def state_label(state: str) -> str:
        if state == "gpu":
            return i18n.t("tray.state_gpu")
        if state == "cpu":
            return i18n.t("tray.state_cpu")
        if state == "off":
            return i18n.t("tray.state_stopped")
        return i18n.t("tray.state_unknown")

    tray = QSystemTrayIcon(app_icon)
    tray.setToolTip(i18n.t("tray.tooltip_stopped"))
    tray.setVisible(True)

    current_state = {"value": "off", "provider": "unloaded"}

    def _notify_event(kind: notifications.EventKind, title_key: str,
                     body_key: str, **kwargs) -> None:
        """Fire a notification event through the manager.

        Used for state-change bubbles (e.g. service just stopped,
        engine just loaded on GPU). The active backend decides how
        to display them — in custom mode the bubble appears in the
        tray, in system mode notify-send is used.
        """
        try:
            notifications.notify_custom(
                title_key, body_key, kind=kind, **kwargs
            )
        except Exception as exc:
            log.warning("notification failed: %s", exc)

    def refresh() -> None:
        payload = _refresh_status()
        state = state_from_payload(payload)
        new_state = state
        old_state = current_state["value"]
        current_state["value"] = state
        current_state["provider"] = payload.get("provider", "unloaded")
        tray.setIcon(make_overlay_icon(app_icon, state))
        if state == "off":
            tray.setToolTip(i18n.t("tray.tooltip_stopped"))
        else:
            tray.setToolTip(i18n.t("tray.tooltip_running", state=state_label(state)))

        # Fire a notification whenever the service transitions
        # between "off" and any running state.  The 0.5 s socket
        # probe is fast, so this happens within one refresh
        # iteration (the refresh timer runs every 2 s).
        is_running = state != "off"
        was_running = old_state != "off"
        if is_running and not was_running:
            _notify_event(
                notifications.EventKind.SUCCESS,
                "notifications.title.service",
                "notifications.started",
            )
        elif not is_running and was_running:
            _notify_event(
                notifications.EventKind.INFO,
                "notifications.title.service",
                "notifications.stopped",
            )

        # Drain notification requests dropped by the CLI / main
        # process (e.g. ``maiocr run`` after a successful OCR).
        # This is the IPC path that lets the tray's custom bubble
        # appear even when the event was generated in a different
        # process.
        _drain_notification_requests()

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def _info_dialog(title: str, body: str) -> None:
        dlg = QDialog()
        dlg.setWindowTitle(title)
        dlg.setWindowIcon(app_icon)
        dlg.resize(560, 420)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit(dlg)
        view.setReadOnly(True)
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        view.setFont(font)
        view.setPlainText(body)
        layout.addWidget(view)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(dlg.accept)
        buttons.accepted.connect(dlg.accept)
        # also offer "Refresh" to re-query
        refresh_btn = QPushButton(i18n.t("actions.refresh"))
        refresh_btn.clicked.connect(lambda: _refresh_then_redraw(dlg, view, title))
        buttons.addButton(refresh_btn, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)
        dlg.exec()

    def _refresh_then_redraw(dlg, view: QPlainTextEdit, title: str) -> None:
        if title == i18n.t("actions.service_status"):
            view.setPlainText(_format_status_text())
        elif title == i18n.t("actions.gpu_info"):
            view.setPlainText(_format_gpu_text())
        dlg.adjustSize()

    def _format_status_text() -> str:
        return format_status_text()

    def _format_gpu_text() -> str:
        return format_gpu_text()

    # Public callbacks (also imported by tests). These wrap the
    # module-level helpers so the dialog is opened with the latest
    # text, then `refresh()` re-reads server status.
    def show_status() -> None:
        try:
            _info_dialog(i18n.t("actions.service_status"), _format_status_text())
        except Exception as exc:
            notifications.notify_error(f"{exc}")

    def show_gpu_info() -> None:
        try:
            _info_dialog(i18n.t("actions.gpu_info"), _format_gpu_text())
        except Exception as exc:
            notifications.notify_error(f"{exc}")

    def release_vram() -> None:
        # Call the module-level implementation, then refresh the icon.
        globals()["release_vram"]()
        refresh()

    def restart_service() -> None:
        globals()["restart_service"]()
        refresh()

    def start_service() -> None:
        globals()["start_service"]()
        refresh()

    def stop_service() -> None:
        globals()["stop_service"]()
        refresh()

    def run_ocr() -> None:
        globals()["run_ocr"]()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    menu = QMenu()

    def add_action(label: str, slot) -> QAction:
        act = QAction(label, menu)
        act.triggered.connect(lambda _checked=False: slot())
        menu.addAction(act)
        return act

    def add_separator() -> None:
        menu.addSeparator()

    add_action(i18n.t("actions.run_ocr"), run_ocr)
    add_action(i18n.t("actions.release_vram"), release_vram)
    add_separator()
    add_action(i18n.t("actions.start"), start_service)
    add_action(i18n.t("actions.stop"), stop_service)
    add_action(i18n.t("actions.restart"), restart_service)
    add_separator()
    add_action(i18n.t("actions.service_status"), show_status)
    add_action(i18n.t("actions.gpu_info"), show_gpu_info)
    add_separator()
    add_action(i18n.t("actions.preferences"), lambda: open_preferences())
    add_separator()
    add_action(i18n.t("actions.quit"), app.quit)

    tray.setContextMenu(menu)

    # Tray icon click handling. The user can pick the action for
    # left-click and double-click independently from Preferences.
    # Qt's QSystemTrayIcon fires ``Trigger`` (single-click) first
    # and then ``DoubleClick`` only if a second click arrives in
    # time. To avoid running the single-click action *and* the
    # double-click action for a true double click, we delay the
    # single-click action by ``DOUBLE_CLICK_INTERVAL_MS`` and
    # cancel the pending timer if a double-click arrives.
    DOUBLE_CLICK_INTERVAL_MS = 250

    # ``_invoke_action`` runs the chosen action.  Defined below
    # so we can use the live ``tray`` / ``menu`` / ``app`` objects.
    def _invoke_action(action_id: str) -> None:
        try:
            action_id = tray_actions.normalize(action_id)
            if action_id == "none":
                return
            if action_id == "release_vram":
                release_vram()
            elif action_id == "run_ocr":
                run_ocr()
            elif action_id == "show_status":
                show_status()
            elif action_id == "show_gpu":
                show_gpu_info()
            elif action_id == "restart":
                restart_service()
            elif action_id == "preferences":
                open_preferences()
            elif action_id == "quit":
                app.quit()
            else:
                # Unknown id (should not happen because we validate
                # against the registry).  Fall back to the legacy
                # behaviour: release VRAM.
                release_vram()
        except Exception as exc:
            notifications.notify_error(f"{exc}")

    pending_single_click = {"timer": None}

    def _cancel_pending_single_click() -> None:
        t = pending_single_click["timer"]
        if t is not None:
            t.stop()
            pending_single_click["timer"] = None

    def on_activated(reason) -> None:
        from PyQt6.QtWidgets import QSystemTrayIcon as _QSTI  # type: ignore
        from PyQt6.QtCore import QTimer as _QT  # type: ignore

        cur = settings_mod.get_settings()
        single_id = tray_actions.normalize(cur.click_action_left)
        double_id = tray_actions.normalize(cur.click_action_double)

        if reason == _QSTI.ActivationReason.DoubleClick:
            # Cancel any pending single-click action — this is a
            # real double-click, the user wants the double-click
            # behaviour, not both.
            _cancel_pending_single_click()
            if double_id != "none":
                _invoke_action(double_id)
            return

        if reason == _QSTI.ActivationReason.Trigger:
            # If the user has a double-click action configured and
            # it differs from the single-click action, wait for
            # the double-click timeout to disambiguate.  If both
            # are the same, run immediately to avoid a perceptible
            # delay.
            if double_id != "none" and double_id != single_id:
                # Schedule the single-click action; cancel if a
                # DoubleClick event arrives in time.
                t = _QT()
                t.setSingleShot(True)
                t.setInterval(DOUBLE_CLICK_INTERVAL_MS)
                def _fire():
                    pending_single_click["timer"] = None
                    if single_id != "none":
                        _invoke_action(single_id)
                t.timeout.connect(_fire)
                pending_single_click["timer"] = t
                t.start()
                return
            # No double-click action configured, or both actions are
            # the same: run the single-click action immediately.
            if single_id != "none":
                _invoke_action(single_id)
            return

    tray.activated.connect(on_activated)

    # ------------------------------------------------------------------
    # Preferences dialog
    # ------------------------------------------------------------------
    def open_preferences() -> None:
        dlg = PreferencesDialog()
        dlg.exec()
        refresh()

    class PreferencesDialog(QDialog):
        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setWindowTitle(i18n.t("actions.preferences"))
            self.setWindowIcon(app_icon)
            self.resize(440, 320)
            layout = QFormLayout(self)

            self._language = QComboBox()
            for code, label in i18n.list_languages():
                self._language.addItem(f"{label} ({code})", code)
            cur = settings_mod.get_settings()
            for i in range(self._language.count()):
                if self._language.itemData(i) == cur.language:
                    self._language.setCurrentIndex(i)
                    break
            layout.addRow(QLabel(i18n.t("settings.language") + ":"), self._language)
            layout.addRow(QLabel(i18n.t("settings.language_help")))

            self._auto_release = QCheckBox(i18n.t("settings.auto_release"))
            self._auto_release.setChecked(cur.auto_release_vram)
            layout.addRow(self._auto_release)
            layout.addRow(QLabel(i18n.t("settings.auto_release_help")))

            self._auto_release_seconds = QSpinBox()
            self._auto_release_seconds.setRange(0, 24 * 3600)
            self._auto_release_seconds.setValue(cur.auto_release_seconds)
            layout.addRow(
                QLabel(i18n.t("settings.auto_release_seconds") + ":"),
                self._auto_release_seconds,
            )
            layout.addRow(QLabel(i18n.t("settings.auto_release_seconds_help")))

            self._prefer_gpu = QCheckBox(i18n.t("settings.prefer_gpu"))
            self._prefer_gpu.setChecked(cur.prefer_gpu)
            layout.addRow(self._prefer_gpu)
            layout.addRow(QLabel(i18n.t("settings.prefer_gpu_help")))

            self._notify = QCheckBox(i18n.t("settings.notify_on_copy"))
            self._notify.setChecked(cur.notify_on_copy)
            layout.addRow(self._notify)
            layout.addRow(QLabel(i18n.t("settings.notify_on_copy_help")))

            self._log_level = QComboBox()
            for level in ("debug", "info", "warning", "error"):
                self._log_level.addItem(level, level)
            for i in range(self._log_level.count()):
                if self._log_level.itemData(i) == cur.log_level:
                    self._log_level.setCurrentIndex(i)
                    break
            layout.addRow(
                QLabel(i18n.t("settings.log_level") + ":"), self._log_level
            )
            layout.addRow(QLabel(i18n.t("settings.log_level_help")))

            # Log rotation
            self._log_max_kb = QSpinBox()
            self._log_max_kb.setRange(64, 1024 * 100)  # 64 KiB .. 100 MiB
            self._log_max_kb.setValue(cur.log_max_bytes // 1024)
            self._log_max_kb.setSuffix(" KiB")
            layout.addRow(
                QLabel(i18n.t("settings.log_max_size") + ":"),
                self._log_max_kb,
            )
            layout.addRow(QLabel(i18n.t("settings.log_max_size_help")))

            self._log_backups = QSpinBox()
            self._log_backups.setRange(0, 50)
            self._log_backups.setValue(cur.log_backup_count)
            layout.addRow(
                QLabel(i18n.t("settings.log_backup_count") + ":"),
                self._log_backups,
            )
            layout.addRow(QLabel(i18n.t("settings.log_backup_count_help")))

            self._log_retention = QSpinBox()
            self._log_retention.setRange(0, 365)
            self._log_retention.setValue(cur.log_retention_days)
            self._log_retention.setSuffix(" d")
            layout.addRow(
                QLabel(i18n.t("settings.log_retention_days") + ":"),
                self._log_retention,
            )
            layout.addRow(QLabel(i18n.t("settings.log_retention_days_help")))

            # Notifications
            self._notification_mode = QComboBox()
            for code, label_key in (
                ("system", "settings.notification_mode_system"),
                ("custom", "settings.notification_mode_custom"),
            ):
                self._notification_mode.addItem(i18n.t(label_key), code)
            for i in range(self._notification_mode.count()):
                if self._notification_mode.itemData(i) == cur.notification_mode:
                    self._notification_mode.setCurrentIndex(i)
                    break
            layout.addRow(
                QLabel(i18n.t("settings.notification_mode") + ":"),
                self._notification_mode,
            )
            layout.addRow(QLabel(i18n.t("settings.notification_mode_help")))

            self._notification_duration = QSpinBox()
            self._notification_duration.setRange(1, 60)
            self._notification_duration.setValue(cur.notification_duration)
            self._notification_duration.setSuffix(" s")
            layout.addRow(
                QLabel(i18n.t("settings.notification_duration") + ":"),
                self._notification_duration,
            )
            layout.addRow(QLabel(i18n.t("settings.notification_duration_help")))

            # Tray-icon click actions.  We populate a single
            # ``QComboBox`` per click type, with the entries
            # coming from ``maiocr.tray_actions`` so adding a new
            # action is a one-line change in one place.
            def _build_action_combo(current: str) -> "QComboBox":
                cb = QComboBox()
                for action in tray_actions.all_actions():
                    cb.addItem(i18n.t(action.label_key), action.id)
                # Fall back to "none" if the persisted id is
                # unknown (defensive — the Settings layer already
                # normalises this, but a manual edit of
                # settings.yml should not crash the dialog).
                want = current if tray_actions.is_valid(current) else "none"
                for i in range(cb.count()):
                    if cb.itemData(i) == want:
                        cb.setCurrentIndex(i)
                        break
                return cb

            self._click_action_left = _build_action_combo(cur.click_action_left)
            layout.addRow(
                QLabel(i18n.t("settings.click_action_left") + ":"),
                self._click_action_left,
            )
            layout.addRow(QLabel(i18n.t("settings.click_action_left_help")))

            self._click_action_double = _build_action_combo(
                cur.click_action_double
            )
            layout.addRow(
                QLabel(i18n.t("settings.click_action_double") + ":"),
                self._click_action_double,
            )
            layout.addRow(QLabel(i18n.t("settings.click_action_double_help")))

            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
            ok.setText(i18n.t("actions.save"))
            cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
            cancel.setText(i18n.t("actions.cancel"))
            buttons.accepted.connect(self._on_accept)
            buttons.rejected.connect(self.reject)
            layout.addRow(buttons)

        def _on_accept(self) -> None:
            new_lang = self._language.currentData()
            cur = settings_mod.get_settings()
            cur.update(
                language=new_lang,
                auto_release_vram=self._auto_release.isChecked(),
                auto_release_seconds=self._auto_release_seconds.value(),
                prefer_gpu=self._prefer_gpu.isChecked(),
                notify_on_copy=self._notify.isChecked(),
                log_level=self._log_level.currentData(),
                log_max_bytes=self._log_max_kb.value() * 1024,
                log_backup_count=self._log_backups.value(),
                log_retention_days=self._log_retention.value(),
                notification_mode=self._notification_mode.currentData(),
                notification_duration=self._notification_duration.value(),
                click_action_left=tray_actions.normalize(
                    self._click_action_left.currentData()
                ),
                click_action_double=tray_actions.normalize(
                    self._click_action_double.currentData()
                ),
            )
            settings_mod.save_settings(cur)
            i18n.set_default_language(cur.language)
            # The notification manager keeps a reference to the
            # current active backend; the easiest way to honour a
            # mode change without restarting the tray is to
            # re-initialise the backends.
            notifications.reinit()
            client.set_remote_settings({"language": cur.language})
            self.accept()

    # Refresh timer
    timer = QTimer()
    timer.setInterval(2000)
    timer.timeout.connect(refresh)
    timer.start()
    refresh()
    notifications._mark_event_loop_started()
    notifications.reinit()  # pick up the Qt event-loop flag

    def handle_signal(*_):
        app.quit()

    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except Exception:
        pass

    return app.exec()


# ---------------------------------------------------------------------------
# Headless fallback.
# ---------------------------------------------------------------------------


def _headless_loop(interval: float) -> int:
    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)
    print(
        f"maiocr-tray: headless mode, refreshing {_status_path()} every {interval}s"
    )
    while True:
        try:
            _refresh_status()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"refresh error: {exc}", file=sys.stderr)
        time.sleep(interval)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="maiocr-tray")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="do not open a system tray; just refresh the status file",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    s = settings_mod.get_settings()
    i18n.set_default_language(s.language)
    notifications.initialise()
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_status_file()

    if args.headless:
        return _headless_loop(args.interval)

    rc = _try_qt_tray()
    if rc is None:
        return _headless_loop(args.interval)
    return rc


if __name__ == "__main__":
    sys.exit(main())
