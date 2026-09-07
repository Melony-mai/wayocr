"""Notification subsystem for MaiOCR.

This module provides an event / backend / manager architecture.  The
key design decision for Niri / Wayland is that MaiOCR **never creates
a new Qt toplevel window** for status notifications: Niri treats
each new ``wl_shell_surface`` as a regular window and moves focus
to it, which would be disruptive.

Instead, notifications are delivered through the freedesktop
notification spec over D-Bus
(``org.freedesktop.Notifications``).  This is the standard
Wayland / Niri path for fire-and-forget pop-ups and is rendered
by whatever notification daemon the user has installed
(``mako``, ``dunst``, ``fnott``, the ``quickshell`` SNI host's
built-in notifier, etc.).  The notification appears as a
non-focus-stealing overlay drawn by the daemon.  MaiOCR's own
process never creates a Wayland surface for a notification.

The tray icon itself is still a ``QSystemTrayIcon`` (the SNI
protocol) — that is the standard Wayland tray mechanism and does
not create a new MaiOCR toplevel window either: the SNI host
draws the icon in its reserved area.

OCR capture uses ``slurp`` + ``grim`` — external Wayland tools
that already use the layer-shell protocol themselves, so the
region-selection overlay is not a MaiOCR window either.
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
    # A "silent" event is shown only briefly and does not enter
    # the system notification history.  We mark it with
    # ``transient:1`` so any compliant notification daemon
    # (mako, dunst, fnott, the dms / quickshell built-in notifier)
    # shows it for a couple of seconds and then removes it.
    SILENT = "silent"


@dataclass(frozen=True)
class Event:
    """A logical notification event.

    Same ``Event`` can be displayed by any backend; the backend is
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
# D-Bus backend (freedesktop notification spec)
# ---------------------------------------------------------------------------
#
# The freedesktop notification spec is the standard Wayland / Niri
# notification path.  It is rendered by any notification daemon
# registered on the session D-Bus.  The user does not need to
# install anything specific: on a typical Niri + DankMaterialShell
# setup the DMS / quickshell SNI host also acts as the
# notification service.  We install ``mako`` as part of the
# install script so even a bare-bones Niri session has a
# notification daemon.
# ---------------------------------------------------------------------------


class DBusBackend(Backend):
    """Talks to ``org.freedesktop.Notifications`` directly via D-Bus."""

    name = "dbus"

    def __init__(self) -> None:
        self._available: Optional[bool] = None
        self._bus = None
        self._proxy = None
        self._interface = None

    def is_available(self) -> bool:
        if self._available is None:
            self._available, self._bus, self._proxy, self._interface = self._connect()
        return self._available

    @staticmethod
    def _connect():
        try:
            import dbus  # type: ignore
            from dbus import SessionBus  # type: ignore
        except Exception:
            return False, None, None, None
        try:
            bus = SessionBus()
            proxy = bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
            )
            return True, bus, proxy, proxy
        except Exception as exc:
            log.debug("D-Bus Notifications unavailable: %s", exc)
            return False, None, None, None

    def show(self, event: Event) -> None:
        if not self.is_available():
            log.info(
                "notification (no D-Bus): %s — %s", event.title, event.body
            )
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
                "transient": dbus.Boolean(0),  # type: ignore[attr-defined]
            }
            # Silent events use transient:1 so the daemon displays
            # them briefly and does not add them to the persistent
            # notification history.
            if event.kind is EventKind.SILENT:
                hints["transient"] = dbus.Boolean(1)  # type: ignore[attr-defined]
            # Use the event id as the notification id so subsequent
            # updates replace the previous one (D-Bus
            # ``replaces_id``).
            replaces_id = abs(hash(event.id)) & 0x7FFFFFFF
            iface.Notify(
                "MaiOCR",                  # app_name
                replaces_id,              # replaces_id
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
# notify-send fallback backend
# ---------------------------------------------------------------------------
#
# ``notify-send`` is part of ``libnotify`` and is the simplest
# command-line path.  We use it as a last-resort fallback if D-Bus
# is unavailable (e.g. on a server, or a session without a
# notification daemon registered).  The D-Bus backend is
# preferred on Niri.
# ---------------------------------------------------------------------------


class SystemBackend(Backend):
    """Uses the freedesktop ``notify-send`` tool as a fallback."""

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
            log.info(
                "notification (no notify-send): %s — %s", event.title, event.body
            )
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
            log.debug(
                "no active backend; dropping event id=%s", event.id
            )
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


def initialise() -> None:
    """Register the default backends and select one based on settings.

    Selection rules (in priority order):

    1. ``notification_mode == "dbus"`` and the D-Bus Notifications
       service is reachable → the D-Bus backend.  This is the
       recommended mode on Niri / Wayland because it never creates
       a MaiOCR Wayland surface and respects the user's existing
       notification setup.
    2. ``notification_mode == "system"`` → the ``notify-send``
       backend (always available on a desktop install).
    3. ``notification_mode == "custom"`` is now an alias for
       ``"dbus"``: a custom notification on Niri is just a
       well-formed D-Bus notification.  The user-configured
       duration controls how long the daemon displays it before
       closing the bubble.
    4. If D-Bus is unavailable and the user picked ``dbus`` /
       ``custom`` → fall back to ``notify-send``.

    The active backend is the only one that ever receives an event;
    the other backends are never invoked.
    """
    global _INITIALISED
    with _MANAGER_LOCK:
        s = settings_mod.get_settings()
        mode = s.notification_mode

        null = NullBackend()
        _MANAGER.register(null)

        dbus = DBusBackend()
        if dbus.is_available():
            _MANAGER.register(dbus)

        system = SystemBackend()
        if system.is_available():
            _MANAGER.register(system)

        # Both ``dbus`` and ``custom`` map to the D-Bus backend
        # because the "custom bubble" on Niri is just a normal
        # freedesktop notification rendered by the user's daemon.
        if mode in ("dbus", "custom"):
            if dbus.is_available():
                _MANAGER.set_active("dbus")
            elif system.is_available():
                log.warning(
                    "notification_mode=%s but no D-Bus notification "
                    "service is running; falling back to notify-send",
                    mode,
                )
                _MANAGER.set_active("system")
            else:
                log.warning(
                    "notification_mode=%s but neither D-Bus nor "
                    "notify-send is available; notifications are "
                    "logged only",
                    mode,
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
            EventKind.SUCCESS,
            "notifications.title.ocr",
            "notifications.copied",
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
            EventKind.SUCCESS,
            "notifications.title.gpu",
            "notifications.vram_released",
        )
    )


def notify_vram_already_unloaded() -> None:
    _MANAGER.notify_event(
        _build_event(
            "vram.idle",
            EventKind.INFO,
            "notifications.title.gpu",
            "notifications.vram_already_unloaded",
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
    """Send a notification using two i18n keys."""
    _MANAGER.notify_event(
        _build_event(
            f"custom.{title_key}",
            kind,
            title_key,
            body_key,
            **kwargs,
        )
    )


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
# it cannot render a bubble itself.  We write a small JSON request
# to ``$XDG_RUNTIME_DIR/maiocr/notifications/`` and the tray process
# picks it up on its next refresh, forwards it to the active
# notification backend, and deletes the file.  The notification
# appears in the user's notification daemon (mako, dunst, the
# dms / quickshell built-in notifier) without ever creating a
# MaiOCR Wayland surface.
# ---------------------------------------------------------------------------


def _notify_request_path() -> "Path":
    from maiocr import paths
    paths.NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
    return paths.NOTIFY_DIR


def send_request(event: Event) -> None:
    """Drop a notification request for the tray to pick up."""
    import json
    import os
    import tempfile
    from maiocr import paths

    paths.NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
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
    """Tell the tray to show an OCR-completed notification.

    The actual rendering is delegated to the active backend, so
    the user sees the right thing whether they picked ``system`` or
    ``custom`` (D-Bus) mode.
    """
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode == "system":
        notify_ocr_completed(chars)
        return
    try:
        send_request(
            Event(
                id="ocr.completed.request",
                kind=EventKind.SUCCESS,
                title=i18n.t("notifications.silent_ocr_done", n=chars),
                body=i18n.t("notifications.silent_ocr_done", n=chars),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)


def request_vram_released() -> None:
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode == "system":
        notify_vram_released()
        return
    try:
        send_request(
            Event(
                id="vram.released.request",
                kind=EventKind.SUCCESS,
                title=i18n.t("notifications.title.gpu"),
                body=i18n.t("notifications.vram_released"),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)


def request_vram_already_unloaded() -> None:
    s = settings_mod.get_settings(reload=False)
    if s.notification_mode == "system":
        notify_vram_already_unloaded()
        return
    try:
        send_request(
            Event(
                id="vram.idle.request",
                kind=EventKind.INFO,
                title=i18n.t("notifications.title.gpu"),
                body=i18n.t("notifications.vram_already_unloaded"),
            )
        )
    except Exception as exc:
        log.debug("notify request failed: %s", exc)


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------
# Existing modules do ``from maiocr import notify`` and then
# ``notify.notify(message)``.  Keep that entry point working: it
# routes through the manager.


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
