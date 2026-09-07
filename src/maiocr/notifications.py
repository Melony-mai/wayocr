"""Notification subsystem for MaiOCR.

This module provides an event / backend / manager architecture
inspired by libnotify's design but implemented from scratch so
that MaiOCR has a self-contained bubble that works on **Niri /
Wayland** without depending on gnome-shell / KDE / XFCE
notification daemons.

Architecture
============

* ``Event`` — an immutable logical event (id, kind, title, body).
* ``Backend`` — a protocol with one ``show(event)`` method.
* ``Manager`` — singleton holding a thread-safe registry of
  backends.  ``notify_event`` hands the event to the **single**
  active backend.  The two notification modes (``system`` vs
  ``custom``) are mutually exclusive: the other backends are never
  invoked.

The two backends shipped with MaiOCR:

* ``SystemBackend`` — shells out to ``notify-send`` with the
  freedesktop notification spec hints, including ``transient:1``
  for SILENT events so they do not pollute the system history.
* ``CustomBubbleBackend`` — renders a PyQt6 toplevel window as the
  bubble.  This is the "custom notification bubble" the user
  selects in Preferences.

Niri / Wayland compatibility
============================

The bubble is a real ``QWidget`` with
``Qt.WindowType.Window | Qt.WindowStaysOnTopHint |
Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus`` and a
semi-transparent background.  The position is clamped to fit
within the chosen screen so the bubble is always visible even
when the user has multiple Wayland outputs with a non-trivial
arrangement.

We deliberately avoid ``Qt.WindowType.Tool`` here: under Wayland
(niri, sway) tool windows are often hidden by the compositor or
do not accept ``XDG Shell`` role assignments, so they would
never be visible.  The bubble uses a standard top-level window
which niri's compositor renders like any other application window.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from maiocr import i18n, settings as settings_mod

log = logging.getLogger("maiocr.notifications")


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    # A "silent" event is shown only as a minimal, unobtrusive
    # indicator — the custom backend renders a small in-corner pill
    # and the system backend marks the notify-send call transient.
    SILENT = "silent"


@dataclass(frozen=True)
class Event:
    """A logical notification event.

    The same Event can be displayed by any backend; the backend is
    responsible for choosing how to render ``title`` and ``body``.
    """

    id: str
    kind: EventKind
    title: str
    body: str
    timestamp: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class Backend:
    """A notification backend.

    Backends are stateless w.r.t. routing — they just render one
    event at a time.  The manager owns lifecycle and the active
    instance.
    """

    name: str = "base"

    def show(self, event: Event) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    def shutdown(self) -> None:  # pragma: no cover - default no-op
        return None


# ---------------------------------------------------------------------------
# System backend (notify-send)
# ---------------------------------------------------------------------------


class SystemBackend(Backend):
    """Uses the freedesktop ``notify-send`` tool."""

    name = "system"

    def __init__(self) -> None:
        import shutil
        import subprocess

        self._shutil = shutil
        self._subprocess = subprocess
        self._has_notify_send = bool(shutil.which("notify-send"))
        self._urgency_map = {
            EventKind.INFO: "low",
            EventKind.SUCCESS: "low",
            EventKind.WARNING: "normal",
            EventKind.ERROR: "critical",
        }

    def is_available(self) -> bool:
        return self._has_notify_send

    def show(self, event: Event) -> None:
        if not self._has_notify_send:
            log.info("notification (no notify-send): %s — %s", event.title, event.body)
            return
        urgency = self._urgency_map.get(event.kind, "low")
        cmd = [
            "notify-send",
            "-a",
            i18n.t("app.name"),
            "-u",
            urgency,
            "-h",
            "string:x-canonical-private-synchronous:maiocr",
        ]
        # Silent events must never enter the system notification
        # history.  We use two hints:
        #   transient:1        → does not appear in notification lists
        #   desktop-entry: maiocr → grouped with our app's notifications
        if event.kind is EventKind.SILENT:
            cmd += [
                "-h", "int:transient:1",
                "-h", "string:desktop-entry:maiocr",
            ]
        cmd += [event.title, event.body]
        try:
            self._subprocess.run(cmd, check=False, timeout=5)
        except Exception as exc:
            log.warning("notify-send failed: %s", exc)


# ---------------------------------------------------------------------------
# D-Bus fallback backend
# ---------------------------------------------------------------------------


class DBusBackend(Backend):
    """Talks to ``org.freedesktop.Notifications`` directly via D-Bus.

    This is a Wayland-friendly fallback that works on niri, KDE,
    GNOME, sway and any environment with a notification daemon.
    Unlike ``SystemBackend`` it does not need ``notify-send`` on
    the ``PATH`` and does not shell out — it speaks the protocol
    directly, which makes it a better fit for long-running daemons.
    """

    name = "dbus"

    def __init__(self) -> None:
        self._available: Optional[bool] = None
        self._proxy = None

    def is_available(self) -> bool:
        if self._available is None:
            self._available, self._proxy = self._connect()
        return self._available

    @staticmethod
    def _connect():
        try:
            import dbus  # type: ignore
            import dbus.mainloop  # type: ignore  # noqa: F401
            from dbus import SessionBus  # type: ignore
        except Exception:
            return False, None
        try:
            bus = SessionBus()
            proxy = bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
            )
            return True, proxy
        except Exception:
            return False, None

    def show(self, event: Event) -> None:
        if not self.is_available():
            log.info("notification (no dbus): %s — %s", event.title, event.body)
            return
        try:
            import dbus  # type: ignore
            iface = dbus.Interface(
                self._proxy, "org.freedesktop.Notifications"
            )
            urgency_map = {
                EventKind.INFO: 0,        # low
                EventKind.SUCCESS: 0,     # low
                EventKind.WARNING: 1,     # normal
                EventKind.ERROR: 2,       # critical
                EventKind.SILENT: 0,      # low
            }
            hints = {
                "x-canonical-private-synchronous": "maiocr",
                "desktop-entry": "maiocr",
            }
            if event.kind is EventKind.SILENT:
                hints["transient"] = 1  # type: ignore[assignment]
            iface.Notify(
                "MaiOCR",                  # app_name
                0,                         # replaces_id
                "",                        # app_icon
                event.title,               # summary
                event.body,                # body
                [],                        # actions
                hints,                     # hints
                urgency_map.get(event.kind, 0),
            )
        except Exception as exc:
            log.warning("D-Bus notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Custom bubble backend (PyQt6)
# ---------------------------------------------------------------------------


def _screen_geometry_for_point(point) -> "QRect | None":
    """Return the geometry of the screen that contains ``point``,
    falling back to the primary screen if the point does not fall
    on any known screen.

    This is Wayland-safe: ``QApplication.screenAt(point)`` asks the
    compositor which output a given pixel lives on, unlike
    ``primaryScreen().availableGeometry()`` which can return a
    composite virtual rectangle.
    """
    from PyQt6.QtWidgets import QApplication  # type: ignore

    app = QApplication.instance()
    if app is None:
        return None
    screen = app.screenAt(point)
    if screen is None:
        screen = app.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()


class CustomBubbleBackend(Backend):
    """A custom PyQt6 notification bubble.

    The bubble is a frameless, always-on-top, translucent window
    that appears in the bottom-right corner of the screen that
    contains the tray icon.  It fades in, holds for the configured
    duration, then fades out.  Multiple bubbles stack vertically.

    On Wayland (niri, sway) the bubble is a real top-level window
    with ``Qt.WindowType.Window`` and ``WindowStaysOnTopHint`` —
    niri renders it like any other application window.  We avoid
    ``Qt.WindowType.Tool`` because Wayland compositors hide tool
    windows by default.
    """

    name = "custom"

    def __init__(self, duration_seconds: int) -> None:
        self._duration = max(1, int(duration_seconds))
        self._app = None
        self._screen = None
        self._bubble_class = None
        self._silent_class = None
        # Queue of active bubbles so we can dismiss them all on
        # shutdown, and so the size policy of the widget class is
        # set before any instance is created.
        self._active_widgets: list = []
        self._ensure_qt()

    def _ensure_qt(self) -> None:
        if self._bubble_class is not None:
            return
        try:
            from PyQt6.QtCore import (  # type: ignore
                QEasingCurve,
                QPoint,
                QPropertyAnimation,
                QRect,
                QSize,
                Qt,
                QTimer,
            )
            from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap  # type: ignore
            from PyQt6.QtWidgets import (  # type: ignore
                QApplication,
                QHBoxLayout,
                QLabel,
                QSizePolicy,
                QVBoxLayout,
                QWidget,
            )
        except Exception as exc:
            log.debug("PyQt6 not available for custom bubble: %s", exc)
            self._bubble_class = None
            return

        from PyQt6.QtCore import Qt as _Qt  # type: ignore

        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)

        icon_path = self._find_icon()
        icon: Optional[QIcon] = QIcon(str(icon_path)) if icon_path else None
        if icon is not None and not icon.isNull():
            self._app.setWindowIcon(icon)

        OuterBubble, SilentIndicator = self._make_widgets(
            QWidget, QLabel, QHBoxLayout, QVBoxLayout,
            QPainter, QColor, QFont, QPixmap, QRect, QSize, QPoint,
            QPropertyAnimation, QEasingCurve, QTimer, _Qt, QApplication,
            QSizePolicy,
        )
        self._bubble_class = OuterBubble
        self._silent_class = SilentIndicator

    def _find_icon(self):
        from pathlib import Path
        from maiocr import resources
        return resources.find_icon("icon.ico")

    @staticmethod
    def _make_widgets(
        QWidget, QLabel, QHBoxLayout, QVBoxLayout,
        QPainter, QColor, QFont, QPixmap, QRect, QSize, QPoint,
        QPropertyAnimation, QEasingCurve, QTimer, Qt, QApplication,
        QSizePolicy,
    ):
        """Build both widget classes.  Defined as a static method so
        the imported PyQt6 names are captured in the closure
        rather than being looked up dynamically at show time.
        """

        # The number of pixels we leave between the bubble and the
        # edge of the screen.  Tuned for niri at 1.0 device pixel
        # ratio.  On HiDPI this looks slightly smaller in device
        # pixels, but the bubble scales with the device pixel ratio
        # so the visual size is consistent.
        SCREEN_MARGIN = 24

        def _clamp_to_screen(widget, geometry):
            """Position ``widget`` so that it fits within the given
            screen ``geometry`` (a ``QRect``).  The widget is
            anchored to the bottom-right corner.
            """
            # Defensive: if the geometry is empty (e.g. headless),
            # leave the widget where Qt put it.
            if geometry is None or geometry.width() <= 0 or geometry.height() <= 0:
                return
            x = geometry.right() - widget.width() - SCREEN_MARGIN
            y = geometry.bottom() - widget.height() - SCREEN_MARGIN
            # Clamp to the top-left of the geometry so the bubble is
            # never off-screen, even if it is wider than the screen.
            x = max(geometry.left() + SCREEN_MARGIN, x)
            y = max(geometry.top() + SCREEN_MARGIN, y)
            widget.move(x, y)

        def _position_bottom_right(widget):
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return
            screen = app.primaryScreen()
            if screen is None:
                return
            _clamp_to_screen(widget, screen.availableGeometry())

        def _fade_out(widget, duration_ms: int = 220) -> None:
            if getattr(widget, "_closed", False):
                return
            widget._closed = True
            anim = QPropertyAnimation(widget, b"windowOpacity")
            anim.setDuration(duration_ms)
            anim.setStartValue(widget.windowOpacity())
            anim.setEndValue(0.0)
            anim.finished.connect(widget.close)
            anim.start()

        class _Bubble(QWidget):
            """Regular bubble — a card with title + body."""

            _BUBBLE_CSS = """
                #maiocrBubbleCard {
                    background-color: rgba(28, 28, 32, 235);
                    border: 1px solid rgba(140, 140, 160, 140);
                    border-radius: 12px;
                }
                QLabel#maiocrBubbleTitle {
                    color: white;
                    font-weight: bold;
                    background: transparent;
                }
                QLabel#maiocrBubbleBody {
                    color: rgba(225, 225, 235, 230);
                    background: transparent;
                }
                QLabel#maiocrBubbleIcon {
                    background: transparent;
                }
            """

            def __init__(self, title, body, kind, duration_ms, app_icon,
                         QPainter=QPainter, QColor=QColor, QFont=QFont,
                         QPixmap=QPixmap, QRect=QRect, QSize=QSize,
                         QPoint=QPoint, QPropertyAnimation=QPropertyAnimation,
                         QEasingCurve=QEasingCurve, QTimer=QTimer,
                         Qt=Qt, QLabel=QLabel, QHBoxLayout=QHBoxLayout,
                         QVBoxLayout=QVBoxLayout, QWidget=QWidget,
                         QApplication=QApplication,
                         QSizePolicy=QSizePolicy):
                super().__init__()
                self._kind = kind
                self._duration_ms = int(duration_ms)
                self._closed = False

                # Use a normal top-level window so niri / Wayland
                # render it like any other application window.
                # ``WindowDoesNotAcceptFocus`` keeps the user's
                # current focus untouched.  ``FramelessWindowHint``
                # removes the title bar.  ``WindowStaysOnTopHint``
                # keeps the bubble above other windows.
                self.setWindowFlags(
                    Qt.WindowType.Window
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus
                )
                # Translucent so the rounded card blends in.  Both
                # the Wayland ``xdg-shell`` protocol and X11 support
                # this attribute.
                self.setAttribute(
                    Qt.WidgetAttribute.WA_TranslucentBackground, True
                )
                self.setAttribute(
                    Qt.WidgetAttribute.WA_ShowWithoutActivating, True
                )
                self.setAttribute(
                    Qt.WidgetAttribute.WA_DeleteOnClose, True
                )

                # Card widget holds the visible content.  We
                # constrain its size to keep the bubble small
                # (≤360 px wide).  The outer window is sized to
                # the card via ``adjustSize()``.
                card = QWidget(self)
                card.setObjectName("maiocrBubbleCard")
                card.setStyleSheet(self._BUBBLE_CSS)
                # Critical: the card must size to its content and
                # not be allowed to stretch.  Without this, the
                # outer window grows to fill the available width.
                card.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )
                card.setMaximumWidth(360)
                card.setMinimumWidth(0)

                layout = QHBoxLayout(card)
                layout.setContentsMargins(12, 12, 12, 12)
                layout.setSpacing(10)

                if app_icon is not None and not app_icon.isNull():
                    icon_label = QLabel(card)
                    icon_label.setObjectName("maiocrBubbleIcon")
                    icon_label.setPixmap(
                        app_icon.pixmap(QSize(36, 36))
                    )
                    layout.addWidget(icon_label, 0)

                text = QVBoxLayout()
                text.setContentsMargins(0, 0, 0, 0)
                text.setSpacing(2)
                title_label = QLabel(title, card)
                title_label.setObjectName("maiocrBubbleTitle")
                title_font = title_label.font()
                title_font.setPointSize(11)
                title_font.setBold(True)
                title_label.setFont(title_font)
                body_label = QLabel(body, card)
                body_label.setObjectName("maiocrBubbleBody")
                body_label.setWordWrap(True)
                body_label.setMaximumWidth(320)
                body_font = body_label.font()
                body_font.setPointSize(10)
                body_label.setFont(body_font)
                text.addWidget(title_label)
                text.addWidget(body_label)
                text.addStretch(1)
                layout.addLayout(text, 1)

                # Outer layout — just the card.  Margins = 0 so the
                # window is exactly the card's size.
                outer = QVBoxLayout(self)
                outer.setContentsMargins(0, 0, 0, 0)
                outer.setSpacing(0)
                outer.addWidget(card)

                # Force a fixed maximum width on the whole window so
                # the stylesheet can never blow the bubble up to
                # fill the screen.
                self.setMaximumWidth(360)
                self.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )

                # Now size the window to the card.  This walks the
                # layout and figures out the minimum size the
                # card needs to display its children.
                card.adjustSize()
                self.adjustSize()

                # Place the bubble in the bottom-right of the
                # primary screen, clamped to the screen bounds.
                _position_bottom_right(self)
                # Also clamp against the screen that actually
                # contains the tray icon, so a multi-monitor
                # setup with negative coordinates still works.
                geo = _screen_geometry_for_point(self.pos())
                if geo is not None:
                    _clamp_to_screen(self, geo)

                log.info(
                    "bubble: created %r at pos=%s size=%s",
                    f"{title[:24]}|{body[:24]}",
                    self.pos(),
                    self.size(),
                )

                self.show()
                # Force the window to actually appear above the
                # compositor's splash layer.
                self.raise_()

                # Animate opacity in.
                self.setWindowOpacity(0.0)
                self._fade_in = QPropertyAnimation(self, b"windowOpacity")
                self._fade_in.setDuration(180)
                self._fade_in.setStartValue(0.0)
                self._fade_in.setEndValue(1.0)
                self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._fade_in.start()

                # Fade out + close after the configured duration.
                self._timer = QTimer(self)
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(lambda: _fade_out(self))
                self._timer.start(self._duration_ms)

            def mousePressEvent(self, _event):
                _fade_out(self)

            def closeEvent(self, event):
                if getattr(self, "_closed", False):
                    return
                _fade_out(self)
                event.ignore()  # let the fade-out animation run

        class _SilentIndicator(QWidget):
            """A tiny, unobtrusive "task done" pill."""

            def __init__(self, title, duration_ms, kind,
                         QPainter=QPainter, QColor=QColor, QFont=QFont,
                         QPixmap=QPixmap, QRect=QRect, QSize=QSize,
                         QPoint=QPoint, QPropertyAnimation=QPropertyAnimation,
                         QEasingCurve=QEasingCurve, QTimer=QTimer,
                         Qt=Qt, QLabel=QLabel, QHBoxLayout=QHBoxLayout,
                         QVBoxLayout=QVBoxLayout, QWidget=QWidget,
                         QApplication=QApplication,
                         QSizePolicy=QSizePolicy):
                super().__init__()
                self._closed = False
                self._title = title
                self._kind = kind

                self.setWindowFlags(
                    Qt.WindowType.Window
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus
                )
                self.setAttribute(
                    Qt.WidgetAttribute.WA_TranslucentBackground, True
                )
                self.setAttribute(
                    Qt.WidgetAttribute.WA_ShowWithoutActivating, True
                )
                self.setAttribute(
                    Qt.WidgetAttribute.WA_DeleteOnClose, True
                )

                pill = QWidget(self)
                pill.setObjectName("maiocrSilentPill")
                pill.setStyleSheet(
                    """
                    #maiocrSilentPill {
                        background-color: rgba(28, 28, 32, 220);
                        border: 1px solid rgba(140, 140, 160, 120);
                        border-radius: 13px;
                    }
                    QLabel#maiocrSilentLabel {
                        color: rgba(220, 220, 230, 230);
                        background: transparent;
                    }
                    QLabel#maiocrSilentDot {
                        background: transparent;
                    }
                    """
                )
                pill.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )
                hl = QHBoxLayout(pill)
                hl.setContentsMargins(12, 4, 14, 4)
                hl.setSpacing(8)
                dot_color = {
                    EventKind.SUCCESS: QColor(80, 200, 120),
                    EventKind.INFO: QColor(80, 140, 220),
                    EventKind.WARNING: QColor(220, 170, 60),
                    EventKind.ERROR: QColor(220, 90, 90),
                    EventKind.SILENT: QColor(120, 200, 160),
                }.get(kind, QColor(120, 200, 160))
                dot = QLabel("\u25cf", pill)  # ●
                dot.setObjectName("maiocrSilentDot")
                dot.setStyleSheet(
                    f"color: {dot_color.name()}; font-size: 16px; "
                    f"background: transparent;"
                )
                hl.addWidget(dot, 0)
                label = QLabel(title, pill)
                label.setObjectName("maiocrSilentLabel")
                f = label.font()
                f.setPointSize(10)
                label.setFont(f)
                hl.addWidget(label, 0)

                outer = QVBoxLayout(self)
                outer.setContentsMargins(0, 0, 0, 0)
                outer.setSpacing(0)
                outer.addWidget(pill)

                self.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )
                pill.adjustSize()
                self.adjustSize()
                # Place the pill in the bottom-right, above the
                # regular bubble area.
                _position_bottom_right(self)
                self.move(self.x(), max(0, self.y() - 40))
                geo = _screen_geometry_for_point(self.pos())
                if geo is not None:
                    _clamp_to_screen(self, geo)
                self.show()
                self.raise_()

                self.setWindowOpacity(0.0)
                self._fade_in = QPropertyAnimation(self, b"windowOpacity")
                self._fade_in.setDuration(150)
                self._fade_in.setStartValue(0.0)
                self._fade_in.setEndValue(1.0)
                self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._fade_in.start()

                self._timer = QTimer(self)
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(lambda: _fade_out(self, 320))
                hold_ms = max(1200, min(duration_ms, 4000))
                self._timer.start(hold_ms)

        return _Bubble, _SilentIndicator

    def is_available(self) -> bool:
        return (
            self._bubble_class is not None
            and self._silent_class is not None
            and self._app is not None
        )

    def show(self, event: Event) -> None:
        if not self.is_available():
            log.debug("custom bubble not available; falling back to log only")
            log.info("notification: %s — %s", event.title, event.body)
            return
        from PyQt6.QtCore import QTimer

        def _create():
            try:
                if event.kind is EventKind.SILENT:
                    w = self._silent_class(
                        title=event.title,
                        duration_ms=self._duration * 1000,
                        kind=event.kind,
                    )
                else:
                    w = self._bubble_class(
                        title=event.title,
                        body=event.body,
                        kind=event.kind,
                        duration_ms=self._duration * 1000,
                        app_icon=self._app.windowIcon(),
                    )
                self._active_widgets.append(w)
            except Exception as exc:
                log.warning("custom widget create failed: %s", exc)

        app = self._app
        if app is None:
            return
        if QTimer is not None:
            QTimer.singleShot(0, _create)
        else:
            _create()

    def shutdown(self) -> None:
        if self._app is not None:
            try:
                for w in list(self._app.topLevelWidgets()):
                    if w.isVisible() and w.__class__.__name__ in {
                        "_Bubble", "_SilentIndicator"
                    }:
                        w.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Null backend (for tests and headless mode)
# ---------------------------------------------------------------------------


class NullBackend(Backend):
    name = "null"

    def __init__(self) -> None:
        self.events: List[Event] = []

    def show(self, event: Event) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class Manager:
    """Routes events to the currently active backend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backends: Dict[str, Backend] = {}
        self._active_name: Optional[str] = None

    def register(self, backend: Backend, *, set_active: bool = False) -> None:
        with self._lock:
            self._backends[backend.name] = backend
            if set_active or self._active_name is None:
                self._active_name = backend.name

    def set_active(self, name: str) -> None:
        with self._lock:
            if name not in self._backends:
                raise KeyError(f"unknown backend: {name}")
            self._active_name = name

    def get_active_name(self) -> Optional[str]:
        with self._lock:
            return self._active_name

    def get(self, name: str) -> Optional[Backend]:
        with self._lock:
            return self._backends.get(name)

    def available_backends(self) -> List[str]:
        with self._lock:
            return [
                n
                for n, b in self._backends.items()
                if b.is_available()
            ]

    def notify_event(self, event: Event) -> None:
        with self._lock:
            backend = self._backends.get(self._active_name or "")
        if backend is None:
            log.debug("no active backend; dropping event id=%s", event.id)
            return
        try:
            backend.show(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("backend %s failed: %s", backend.name, exc)


# Singleton manager + a lock around its lifecycle.
_MANAGER = Manager()
_MANAGER_LOCK = threading.Lock()
_INITIALISED = False


def get_manager() -> Manager:
    return _MANAGER


def _has_qt_event_loop() -> bool:
    """True if this process owns a QApplication that is running
    its event loop.
    """
    try:
        from PyQt6.QtWidgets import QApplication  # type: ignore
    except Exception:
        return False
    app = QApplication.instance()
    if app is None:
        return False
    return getattr(app, "_maiocr_event_loop_started", False) is True


def _mark_event_loop_started() -> None:
    """Called by the tray after it starts the Qt event loop."""
    try:
        from PyQt6.QtWidgets import QApplication  # type: ignore
    except Exception:
        return
    app = QApplication.instance()
    if app is not None:
        app._maiocr_event_loop_started = True  # type: ignore[attr-defined]


def initialise() -> None:
    """Register the default backends and select one based on settings.

    Selection rules:

    * Tray process with a running Qt event loop and
      ``notification_mode == "custom"`` → custom-bubble backend
      is active.  Bubbles are shown in-process.
    * Tray process in custom mode without a Qt event loop → D-Bus
      backend (real freedesktop notification, ``transient:1`` so it
      does not pollute history).  This is the "we cannot show our
      own widget, fall back to something visible" branch and should
      not trigger in a normal Niri session.
    * CLI process (no event loop) or ``notification_mode ==
      "system"`` → system backend (notify-send) is active.
    * If ``notification_mode == "custom"`` but PyQt6 is missing
      entirely, fall back to the system backend so the user still
      gets a visible notification.

    The active backend is the only one that ever receives an
    event; the other backends are never invoked, so the two modes
    are mutually exclusive.
    """
    global _INITIALISED
    with _MANAGER_LOCK:
        s = settings_mod.get_settings()
        mode = s.notification_mode

        system = SystemBackend()
        _MANAGER.register(system)
        null = NullBackend()
        _MANAGER.register(null)
        # Always register the D-Bus backend so the ``custom`` mode
        # can fall back to it if PyQt6 is present but the bubble
        # cannot be rendered for any reason.
        dbus = DBusBackend()
        if dbus.is_available():
            _MANAGER.register(dbus)

        if mode == "custom" and _has_qt_event_loop():
            candidate = CustomBubbleBackend(s.notification_duration)
            if candidate.is_available():
                _MANAGER.register(candidate)
                _MANAGER.set_active("custom")
            else:
                log.warning(
                    "notification_mode=custom but PyQt6 is not "
                    "available; falling back to D-Bus notifications"
                )
                _MANAGER.set_active("dbus" if dbus.is_available() else "system")
        elif mode == "custom" and not _has_qt_event_loop():
            # Custom mode but the running process has no Qt event
            # loop (e.g. a CLI).  We still want the user to see
            # something visible, so fall back to the D-Bus
            # backend (real freedesktop notification with
            # ``transient:1`` so it does not pollute history).
            if dbus.is_available():
                log.debug(
                    "notification_mode=custom in a process without a "
                    "Qt event loop; falling back to D-Bus backend"
                )
                _MANAGER.set_active("dbus")
            else:
                log.debug(
                    "notification_mode=custom in a process without a "
                    "Qt event loop and no D-Bus; using silent backend"
                )
                _MANAGER.set_active("null")
        else:
            _MANAGER.set_active("system")

        if not _INITIALISED:
            log.info(
                "notification manager initialised: mode=%s active=%s "
                "available=%s",
                mode,
                _MANAGER.get_active_name(),
                _MANAGER.available_backends(),
            )
        _INITIALISED = True


def reinit() -> None:
    initialise()


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def _build_event(
    event_id: str,
    kind: EventKind,
    title_key: str,
    body_key: str,
    **kwargs: Any,
) -> Event:
    return Event(
        id=event_id,
        kind=kind,
        title=i18n.t(title_key, **kwargs),
        body=i18n.t(body_key, **kwargs),
    )


def notify_service_started() -> None:
    _MANAGER.notify_event(
        _build_event(
            "service.started",
            EventKind.SUCCESS,
            "notifications.title.service",
            "notifications.started",
        )
    )


def notify_service_stopped() -> None:
    _MANAGER.notify_event(
        _build_event(
            "service.stopped",
            EventKind.INFO,
            "notifications.title.service",
            "notifications.stopped",
        )
    )


def notify_service_restarted() -> None:
    _MANAGER.notify_event(
        _build_event(
            "service.restarted",
            EventKind.INFO,
            "notifications.title.service",
            "notifications.restarted",
        )
    )


def notify_ocr_completed(chars: int) -> None:
    _MANAGER.notify_event(
        _build_event(
            "ocr.completed",
            EventKind.SILENT,
            "notifications.silent_ocr_done",
            "notifications.silent_ocr_done",
            n=chars,
        )
    )


def notify_ocr_empty() -> None:
    _MANAGER.notify_event(
        _build_event(
            "ocr.empty",
            EventKind.WARNING,
            "notifications.title.ocr",
            "notifications.no_text",
        )
    )


def notify_vram_released() -> None:
    _MANAGER.notify_event(
        _build_event(
            "vram.released",
            EventKind.SILENT,
            "notifications.silent_vram_released",
            "notifications.silent_vram_released",
        )
    )


def notify_vram_already_unloaded() -> None:
    _MANAGER.notify_event(
        _build_event(
            "vram.idle",
            EventKind.SILENT,
            "notifications.silent_nothing_to_release",
            "notifications.silent_nothing_to_release",
        )
    )


def notify_error(message: str) -> None:
    _MANAGER.notify_event(
        Event(
            id="error",
            kind=EventKind.ERROR,
            title=i18n.t("notifications.title.error"),
            body=message,
        )
    )


def notify_silent(title_key: str, **kwargs: Any) -> None:
    _MANAGER.notify_event(
        Event(
            id=f"silent.{title_key}",
            kind=EventKind.SILENT,
            title=i18n.t(title_key, **kwargs),
            body=i18n.t(title_key, **kwargs),
        )
    )


def notify_custom(title_key: str, body_key: str, *,
                 kind: EventKind = EventKind.INFO, **kwargs: Any) -> None:
    """Send a notification using two i18n keys.

    ``kind`` controls the bubble's accent colour (system
    notifications get a matching urgency).  Extra ``**kwargs`` are
    passed to the i18n formatter.
    """
    _MANAGER.notify_event(
        _build_event(
            f"custom.{title_key}",
            kind,
            title_key,
            body_key,
            **kwargs,
        )
    )


# Backwards-compat shim: existing modules call ``notify.notify(message)``.
# Keep that entry point working but route it through the manager.

def notify(message: str, title: str | None = None) -> None:  # noqa: A001
    if title is None:
        title = i18n.t("app.name")
    _MANAGER.notify_event(
        Event(
            id="legacy",
            kind=EventKind.INFO,
            title=title,
            body=message,
        )
    )


# Helper for tests
def reset_for_tests() -> None:
    global _MANAGER, _INITIALISED
    with _MANAGER_LOCK:
        _MANAGER = Manager()
        _INITIALISED = False


# ---------------------------------------------------------------------------
# Cross-process notification requests.
# ---------------------------------------------------------------------------
#
# The CLI / ``maiocr run`` process does not own a Qt event loop, so
# it cannot show the custom bubble itself.  Instead, it writes a
# small JSON request to ``$XDG_RUNTIME_DIR/maiocr/notifications/``
# and the tray process picks it up on its next refresh, shows the
# bubble, and deletes the file.  This way the user's "custom
# notification bubble" mode works no matter which process fires
# the event.
#
# The request format is just an ``Event`` serialised to JSON.  The
# tray's notification backend takes care of the rest.
# ---------------------------------------------------------------------------


def _notify_request_path() -> "Path":
    from maiocr import paths
    paths.NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
    return paths.NOTIFY_DIR


def send_request(event: Event) -> None:
    """Drop a notification request for the tray to pick up.

    Called from CLI / main processes.  Safe to call when the tray
    is not running: the request file just stays in the directory
    and is cleaned up the next time the tray starts.
    """
    import json
    import os
    import tempfile
    from maiocr import paths

    paths.NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
    # Use the event id (sanitised) as the filename.
    name = "".join(
        c if c.isalnum() or c in ("-", "_", ".") else "_"
        for c in str(event.id)
    ) or "request"
    fd, path = tempfile.mkstemp(
        prefix=f"{int(event.timestamp * 1000)}.{name}.",
        suffix=".json",
        dir=str(paths.NOTIFY_DIR),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(
                {
                    "id": event.id,
                    "kind": event.kind.value,
                    "title": event.title,
                    "body": event.body,
                    "timestamp": event.timestamp,
                },
                fh,
                ensure_ascii=False,
            )
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def request_ocr_completed(chars: int) -> None:
    """Tell the tray to show an OCR-completed bubble.

    Called from the ``maiocr`` client / ``main`` process after a
    successful OCR.  No-op if the user picked the ``system``
    notification mode (because the SystemBackend will already show
    a system notification in the same process) and no-op in the
    custom mode if there is no tray process to pick the request
    up.
    """
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode != "custom":
        return
    try:
        send_request(
            Event(
                id="ocr.completed.request",
                kind=EventKind.SILENT,
                title=i18n.t("notifications.silent_ocr_done", n=chars),
                body=i18n.t("notifications.silent_ocr_done", n=chars),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)


def request_vram_released() -> None:
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode != "custom":
        return
    try:
        send_request(
            Event(
                id="vram.released.request",
                kind=EventKind.SILENT,
                title=i18n.t("notifications.silent_vram_released"),
                body=i18n.t("notifications.silent_vram_released"),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)


def request_vram_already_unloaded() -> None:
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode != "custom":
        return
    try:
        send_request(
            Event(
                id="vram.idle.request",
                kind=EventKind.SILENT,
                title=i18n.t("notifications.silent_nothing_to_release"),
                body=i18n.t("notifications.silent_nothing_to_release"),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)
