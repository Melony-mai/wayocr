"""Backwards-compat shim.

The notification subsystem now lives in :mod:`maiocr.notifications`
(this module is the deprecated public name). Existing callers do
``from maiocr import notify`` and then ``notify.notify(message)`` —
we keep that working by re-exporting the same surface.
"""
from __future__ import annotations

from maiocr.notifications import (  # noqa: F401
    CustomBubbleBackend,
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
    reset_for_tests,
)
