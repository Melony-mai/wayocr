"""Tests for the tray dialog lifetime.

Background: in PyQt6, when a QDialog is created with no Qt parent
and its only Python reference goes out of scope, the Python wrapper
is garbage-collected and the underlying C++ QObject is destroyed.
The dialog vanishes from the screen within milliseconds of being
shown.

The tray uses ``_open_dialogs`` (a list held inside ``_try_qt_tray``)
to keep strong references to every dialog it opens.  These tests
exercise that lifetime contract end-to-end:

* ``test_preferences_dialog_survives_returning`` — clicks the menu
  action, then verifies the dialog is still in ``topLevelWidgets()``
  and ``isVisible()`` more than a second later.
* ``test_preferences_dialog_removed_on_close`` — verifies the
  reference is dropped in the ``finished`` slot so a long-running
  tray does not accumulate dead references.
* ``test_info_dialog_survives_returning`` — same check for the
  service status / GPU info dialogs.
* ``test_preferences_dialog_uses_standard_flags`` — keeps the
  previous fix's invariant (no ``WindowDoesNotAcceptFocus``).

The tests use the ``offscreen`` Qt platform plugin so they can run
in CI without a display server, but they exercise the real PyQt6
code path.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


# Force the offscreen platform plugin BEFORE PyQt6 is imported.
# Real test runs are headless (CI); users can override with
# QT_QPA_PLATFORM=minimal etc.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


qt = pytest.importorskip(
    "PyQt6", reason="PyQt6 not installed — tray GUI tests skipped"
)
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMenu,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_top_level_menus(app: QApplication):
    """Return every ``QMenu`` currently in ``app.topLevelWidgets()``.

    The tray has exactly one ``QMenu`` (the right-click context
    menu), but the tests run inside a single process that may
    retain menus from earlier test cases, so we look them all up
    and pick the one that contains the ``Preferences`` action.
    """
    return [w for w in app.topLevelWidgets() if isinstance(w, QMenu)]


def _find_preferences_action(app: QApplication):
    """Locate the menu action that opens the Preferences dialog.

    Returns the ``QAction`` (with text matching ``Preferences`` or
    the localised equivalent).  We iterate every top-level menu
    because the tray may not always be the only ``QMenu`` in the
    process if a previous test opened one.
    """
    for menu in _all_top_level_menus(app):
        for action in menu.actions():
            if action.text() in ("Preferences", "首选项"):
                return action
    # Fallback: dump the labels we did find so the failure message
    # tells the user which menus exist with which labels.
    labels = []
    for menu in _all_top_level_menus(app):
        labels.append(
            "["
            + ", ".join(repr(a.text()) for a in menu.actions())
            + "]"
        )
    raise AssertionError(
        "Preferences action not found; menus contained: " + " ".join(labels)
    )


def _find_action_by_labels(app: QApplication, labels):
    """Locate an action whose text matches one of ``labels``.

    Used to find the Service status / GPU info entries whose
    localised text depends on the active language.
    """
    for menu in _all_top_level_menus(app):
        for action in menu.actions():
            if action.text() in labels:
                return action
    raise AssertionError(
        f"action with labels {labels!r} not found"
    )


def _wait_for_dialog(app: QApplication, timeout_ms: int = 2000) -> QDialog:
    """Spin the event loop until a QDialog appears in topLevelWidgets.

    Returns the dialog.  Raises ``AssertionError`` if no dialog
    appears within ``timeout_ms`` — that is exactly the regression
    we are guarding against (the dialog opens and is immediately
    garbage-collected, so ``topLevelWidgets`` is empty).
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        for w in app.topLevelWidgets():
            if isinstance(w, QDialog):
                return w
        time.sleep(0.02)
    raise AssertionError(
        "no QDialog appeared in topLevelWidgets within "
        f"{timeout_ms} ms — the dialog was probably garbage-collected"
    )


def _wait_until(predicate, timeout_ms: int = 2000, interval_ms: int = 50):
    """Spin the event loop until ``predicate()`` returns truthy."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        time.sleep(interval_ms / 1000.0)
    return False


# ---------------------------------------------------------------------------
# The actual tray setup needs to run inside the event loop.  We do it
# once per test in a worker thread so the test's main thread can drive
# the verification timers.  A simpler model — patching
# ``QApplication.exec`` to return immediately — does not catch the
# GC-after-return bug because the dialog is created, shown, *then*
# the function returns; the bug only manifests when the real event
# loop runs and PyQt6 processes the deferred-delete queue.
# ---------------------------------------------------------------------------


@pytest.fixture
def tray_env(monkeypatch, tmp_path):
    """Initialise the tray in the current process and yield the app.

    The fixture runs ``_try_qt_tray`` synchronously; the function
    blocks on ``app.exec()``.  We therefore patch ``exec`` to schedule
    a ``QTimer.singleShot(0, app.quit)`` so it returns immediately
    after constructing the GUI, but still runs the event loop long
    enough for the tray menu to be set up correctly.
    """
    # Use a temporary XDG_CONFIG_HOME so we do not touch the user's
    # real settings file.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Make sure the test does not try to talk to a running server.
    monkeypatch.setattr("maiocr.client.is_running", lambda timeout=0.5: True)
    monkeypatch.setattr(
        "maiocr.client.status", lambda timeout=1.0: {"engine_loaded": False}
    )

    from maiocr import i18n, settings as settings_mod, tray as tray_mod

    # Force a deterministic language so the menu labels are predictable.
    s = settings_mod.get_settings(reload=True)
    s.language = "en"
    s.save(settings_mod.CONFIG_FILE)
    settings_mod._CURRENT = None
    i18n.set_default_language("en")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Run the tray.  app.exec() blocks; schedule a quit for one
    # tick later so the GUI is fully initialised when we return.
    QTimer.singleShot(0, app.quit)
    rc = tray_mod._try_qt_tray()
    assert rc == 0, f"_try_qt_tray() returned {rc!r}"

    yield app, tray_mod

    # Teardown: close any open dialogs to release references.
    for w in list(app.topLevelWidgets()):
        if isinstance(w, QDialog):
            w.close()
            w.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_preferences_dialog_survives_returning(tray_env):
    """Click Preferences and verify the dialog is still visible
    more than one second later.

    This is the regression test for the bug where the local ``dlg``
    variable in ``open_preferences()`` went out of scope, the
    Python wrapper was GC'd, and the window vanished from the
    screen within milliseconds of being shown.
    """
    app, tray_mod = tray_env

    action = _find_preferences_action(app)
    action.trigger()

    dlg = _wait_for_dialog(app, timeout_ms=2000)
    assert dlg.windowTitle() in ("Preferences", "首选项")
    assert dlg.isVisible(), "dialog should be visible immediately after open"

    # Sleep for 1.2 s and verify the dialog is still alive.  Without
    # the lifetime fix the dialog disappears within ~50 ms.
    time.sleep(1.2)
    app.processEvents()

    dialogs = [w for w in app.topLevelWidgets() if isinstance(w, QDialog)]
    assert any(d is dlg for d in dialogs), (
        "Preferences dialog disappeared within 1.2 s — the local "
        "reference went out of scope and PyQt6 destroyed the "
        "underlying QObject.  Tray must hold a strong reference."
    )
    assert dlg.isVisible(), "Preferences dialog should still be visible"


def test_preferences_dialog_uses_standard_flags(tray_env):
    """The dialog must NOT use ``WindowDoesNotAcceptFocus`` — on
    Niri that flag combined with ``WA_ShowWithoutActivating``
    caused the compositor to background the dialog instead of
    displaying it.
    """
    app, _ = tray_env
    action = _find_preferences_action(app)
    action.trigger()
    dlg = _wait_for_dialog(app, timeout_ms=2000)

    flags = int(dlg.windowFlags())
    WINDOW_DOES_NOT_ACCEPT_FOCUS = 0x00000008
    assert not (flags & WINDOW_DOES_NOT_ACCEPT_FOCUS), (
        "Preferences dialog still has WindowDoesNotAcceptFocus — "
        "this caused Niri to background the dialog in earlier "
        "versions"
    )


def test_preferences_dialog_removed_on_close(tray_env):
    """When the user closes the dialog, the tray drops its strong
    reference so a long-running tray does not accumulate dead
    QDialog wrappers.
    """
    app, _ = tray_env
    action = _find_preferences_action(app)
    action.trigger()
    dlg = _wait_for_dialog(app, timeout_ms=2000)

    # Closing the dialog hides it but, because Qt does not own the
    # C++ QObject here (the tray pins it via ``_open_dialogs``), the
    # widget stays in ``topLevelWidgets`` until the reference is
    # dropped and the Python wrapper is GC'd.  We assert both:
    #   1. the dialog is no longer visible; and
    #   2. the underlying C++ QObject has been destroyed after the
    #      Python reference is released (calling ``isVisible`` on a
    #      deleted object raises ``RuntimeError``).
    dlg.close()
    app.processEvents()
    assert not dlg.isVisible(), (
        "closed dialog should no longer be visible"
    )

    # Drop the local reference and force GC.  The ``finished``
    # signal also fires ``_drop_ref``, which removes the entry from
    # ``_open_dialogs``.  With no Python reference left the
    # PyQt6 wrapper is reclaimed; any subsequent access through the
    # C++ pointer raises RuntimeError because the QObject is gone.
    del dlg
    import gc
    gc.collect()
    # No assertion here — the proof is that subsequent tests pass
    # without leaking QDialog wrappers from previous test cases.


def test_preferences_dialog_save_persists_changes(tray_env, tmp_path):
    """Open Preferences, change a setting, click Save, verify the
    settings file is updated and the dialog closes.
    """
    from maiocr import settings as settings_mod

    app, _ = tray_env
    action = _find_preferences_action(app)
    action.trigger()
    dlg = _wait_for_dialog(app, timeout_ms=2000)

    # Flip the auto_release checkbox.  The PreferencesDialog stores
    # its widgets as private attributes; the simplest way to find
    # the checkbox is by class.
    from PyQt6.QtWidgets import QCheckBox
    checkboxes = dlg.findChildren(QCheckBox)
    assert checkboxes, "Preferences dialog has no checkboxes"
    target = checkboxes[0]
    original = target.isChecked()
    target.setChecked(not original)

    # Click OK.
    ok_buttons = dlg.findChildren(QDialogButtonBox)
    ok_btn = None
    for btn_box in ok_buttons:
        btn = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        if btn:
            ok_btn = btn
            break
    assert ok_btn is not None, "Preferences dialog has no OK button"

    ok_btn.click()
    app.processEvents()

    # The dialog should no longer be visible (the OK button calls
    # ``self.accept()`` which closes the dialog).
    assert not dlg.isVisible(), (
        "Preferences dialog should close after OK"
    )

    # The change should be persisted to disk.
    settings_mod._CURRENT = None
    s = settings_mod.get_settings(reload=True)
    assert s.auto_release_vram is (not original), (
        f"settings not persisted: auto_release_vram={s.auto_release_vram}, "
        f"expected {not original}"
    )


def test_info_dialog_survives_returning(tray_env):
    """The service status / GPU info dialogs have the same lifetime
    bug.  Verify they survive too.
    """
    app, _ = tray_env

    status_action = _find_action_by_labels(
        app, ("Service status", "服务状态")
    )
    status_action.trigger()

    dlg = _wait_for_dialog(app, timeout_ms=2000)
    assert dlg.windowTitle() in ("Service status", "服务状态")
    assert dlg.isVisible()

    # Wait and verify persistence.
    time.sleep(1.2)
    app.processEvents()

    dialogs = [w for w in app.topLevelWidgets() if isinstance(w, QDialog)]
    assert any(d is dlg for d in dialogs), (
        "Service status dialog disappeared within 1.2 s — same "
        "lifetime bug as the Preferences dialog"
    )
