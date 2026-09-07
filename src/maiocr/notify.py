"""Backwards-compat shim for ``maiocr.notify``.

The notification subsystem now lives in
:mod:`maiocr.notifications`; this module re-exports the public
surface for code that does ``from maiocr import notify`` and then
``notify.notify_ocr_completed(...)``, ``notify.notify(message)``,
etc.

The tray and CLI dispatch every notification through the manager
(``maiocr.notifications.get_manager``); the active backend is
selected by the ``notification_mode`` setting and never creates a
new Qt toplevel window on Niri / Wayland.
"""
from __future__ import annotations

from maiocr.notifications import (  # noqa: F401
    Backend,
    DBusBackend,
    Event,
    EventKind,
    Manager,
    NullBackend,
    SystemBackend,
    get_manager,
    initialise,
    notify,
    notify_custom,
    notify_error,
    notify_ocr_completed,
    notify_ocr_empty,
    notify_service_restarted,
    notify_service_started,
    notify_service_stopped,
    notify_silent,
    notify_vram_already_unloaded,
    notify_vram_released,
    reinit,
    request_ocr_completed,
    request_vram_already_unloaded,
    request_vram_released,
    reset_for_tests,
    send_request,
)
