"""Registry of tray-icon click actions.

Each entry is a dict with:

* ``id`` — short, machine-friendly identifier persisted in
  ``settings.yml`` (e.g. ``"release_vram"``),
* ``label_key`` — i18n key under ``settings.click_action_<...>`` for
  the menu / preferences label,
* ``callable`` — zero-argument function that performs the action.
  The callable is expected to be safe to invoke from the Qt main
  thread; if it needs to talk to the running server it can do so
  over the socket (it is safe to call from a separate process or
  thread too — the action runs whatever the click happened to be).

The default actions are intentionally limited to things that are
already implemented elsewhere in MaiOCR.  The mapping from
``id`` to ``callable`` is wired up in :mod:`maiocr.tray` so this
module stays free of Qt imports and is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List


@dataclass(frozen=True)
class TrayAction:
    id: str
    label_key: str


# ---------------------------------------------------------------------------
# Action definitions.  Each entry is (id, label_key).
# Add new actions here, then wire their callable in
# ``maiocr.tray._invoke_action``.
# ---------------------------------------------------------------------------
_TRAY_ACTIONS: List[TrayAction] = [
    TrayAction("none",            "settings.click_action_none"),
    TrayAction("release_vram",    "settings.click_action_release_vram"),
    TrayAction("run_ocr",         "settings.click_action_run_ocr"),
    TrayAction("show_status",     "settings.click_action_show_status"),
    TrayAction("show_gpu",        "settings.click_action_show_gpu"),
    TrayAction("restart",         "settings.click_action_restart"),
    TrayAction("preferences",     "settings.click_action_preferences"),
    TrayAction("quit",            "settings.click_action_quit"),
]


def all_actions() -> List[TrayAction]:
    """Return a copy of the action list (preserves definition order)."""
    return list(_TRAY_ACTIONS)


def is_valid(action_id: str) -> bool:
    return any(a.id == action_id for a in _TRAY_ACTIONS)


def normalize(action_id: str, default: str = "none") -> str:
    """Return ``action_id`` if it is a known id, otherwise ``default``."""
    return action_id if is_valid(action_id) else default
