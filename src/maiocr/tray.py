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
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from maiocr import client, gpu, i18n, notify, resources, settings as settings_mod
from maiocr import paths, system_info


SERVICE = "MaiOCR.service"


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
        notify.notify(i18n.t("errors.server_unreachable"))
        return
    try:
        resp = client.release_vram(timeout=10.0)
    except Exception as exc:
        notify.notify(i18n.t("notifications.error", error=str(exc)))
        return
    if resp is None or resp.get("error"):
        notify.notify(
            i18n.t(
                "notifications.error",
                error=str(resp.get("error") if resp else "no response"),
            )
        )
    elif resp.get("released"):
        notify.notify(i18n.t("notifications.vram_released"))
    else:
        notify.notify(i18n.t("notifications.vram_already_unloaded"))


def restart_service() -> None:
    notify.notify(i18n.t("notifications.restarted"))
    _run_cli("restart")
    time.sleep(0.7)


def start_service() -> None:
    _run_cli("start")
    time.sleep(0.7)
    if client.is_running(timeout=1.0):
        notify.notify(i18n.t("notifications.started"))


def stop_service() -> None:
    _run_cli("stop")
    time.sleep(0.7)
    if not client.is_running(timeout=1.0):
        notify.notify(i18n.t("notifications.stopped"))


def run_ocr() -> None:
    """Spawn the capture-and-OCR client as a separate process."""
    try:
        subprocess.Popen([sys.executable, "-m", "maiocr.cli", "run"])
    except Exception as exc:
        notify.notify(i18n.t("notifications.error", error=str(exc)))


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

    def state_from_payload(payload: dict) -> str:
        provider = payload.get("provider", "unloaded")
        if not payload.get("engine_loaded", False):
            return "off"
        if provider in ("CUDAExecutionProvider", "TensorrtExecutionProvider"):
            return "gpu"
        if provider == "CPUExecutionProvider":
            return "cpu"
        return "unknown"

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

    def refresh() -> None:
        payload = _refresh_status()
        state = state_from_payload(payload)
        current_state["value"] = state
        current_state["provider"] = payload.get("provider", "unloaded")
        tray.setIcon(make_overlay_icon(app_icon, state))
        if state == "off":
            tray.setToolTip(i18n.t("tray.tooltip_stopped"))
        else:
            tray.setToolTip(i18n.t("tray.tooltip_running", state=state_label(state)))

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
            notify.notify(i18n.t("notifications.error", error=str(exc)))

    def show_gpu_info() -> None:
        try:
            _info_dialog(i18n.t("actions.gpu_info"), _format_gpu_text())
        except Exception as exc:
            notify.notify(i18n.t("notifications.error", error=str(exc)))

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

    # Left-click on the tray icon: show status (most users expect
    # that).
    def on_activated(reason) -> None:
        # Trigger == single click; DoubleClick and MiddleClick also
        # show status because there is no other natural action.
        from PyQt6.QtWidgets import QSystemTrayIcon as _QSTI  # type: ignore
        if reason in (
            _QSTI.ActivationReason.Trigger,
            _QSTI.ActivationReason.DoubleClick,
            _QSTI.ActivationReason.MiddleClick,
        ):
            show_status()
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
            )
            settings_mod.save_settings(cur)
            i18n.set_default_language(cur.language)
            client.set_remote_settings({"language": cur.language})
            self.accept()

    # Refresh timer
    timer = QTimer()
    timer.setInterval(2000)
    timer.timeout.connect(refresh)
    timer.start()
    refresh()

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
