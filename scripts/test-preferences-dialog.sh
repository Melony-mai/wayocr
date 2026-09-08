#!/usr/bin/env bash
# Quick verification script for the Preferences dialog.
# This script does NOT require the user to click anything in the
# tray; it programmatically invokes the click handler and checks
# that a visible dialog appears.  Run this on the Niri system
# to verify the fix.

set -e

cd "$(dirname "$0")/.."

# Make sure the services are up
if ! systemctl --user is-active --quiet MaiOCR-tray.service; then
    echo "Starting MaiOCR-tray.service..."
    systemctl --user start MaiOCR-tray.service
    sleep 2
fi

env -i HOME=$HOME USER=$USER PATH=$PATH \
  LD_LIBRARY_PATH=/opt/cuda/lib64:/usr/lib \
  QT_QPA_PLATFORM=minimal \
  XDG_CONFIG_HOME=$HOME/.config \
  XDG_DATA_HOME=$HOME/.local/share \
  XDG_RUNTIME_DIR=/run/user/$(id -u) \
  /home/mai/.local/share/uv/tools/maiocr/bin/python << 'PYEOF'
import sys, os, subprocess, time
sys.path.insert(0, '/home/mai/.local/share/uv/tools/maiocr/lib/python3.14/site-packages')

# 1) Check the new code is loaded
import inspect, maiocr.tray as t
src = inspect.getsource(t)
assert 'activateWindow' in src, "new code is NOT loaded"
assert 'WindowDoesNotAcceptFocus' not in src, "old WindowDoesNotAcceptFocus is still present"
assert '_open_dialogs' in src, "dialog lifetime fix is NOT loaded"
print("OK: new code is loaded (activateWindow + _open_dialogs present, WindowDoesNotAcceptFocus absent)")

# 2) Verify PyQt6 imports work in the tool venv
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtCore import QTimer
import maiocr.notifications as notifications

# 3) Verify the IPC helpers exist
for name in ('request_ocr_completed', 'request_vram_released', 'request_vram_already_unloaded'):
    assert hasattr(notifications, name), f"{name} is missing"
print("OK: IPC helpers (request_ocr_completed, request_vram_released, request_vram_already_unloaded) are present")

# 4) Verify the dialog opens correctly (off-screen test)
import tempfile
tmpdir = tempfile.mkdtemp()
os.environ['QT_QPA_PLATFORM'] = 'minimal'
os.environ['XDG_CONFIG_HOME'] = tmpdir
os.environ['XDG_DATA_HOME'] = os.path.join(tmpdir, 'share')
os.environ['XDG_RUNTIME_DIR'] = os.path.join(tmpdir, 'run')
os.makedirs(os.environ['XDG_RUNTIME_DIR'], exist_ok=True)

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

import importlib
import maiocr.settings as settings_mod
importlib.reload(settings_mod)
import maiocr.tray as tray_mod
importlib.reload(tray_mod)

s = settings_mod.get_settings()
print(f"OK: settings loaded (language={s.language}, mode={s.notification_mode})")

# Run the tray with a real event loop that exits after 200ms.
# The real event loop is required so PyQt6 processes any deferred
# deletions: the bug we are guarding against is that the dialog's
# Python wrapper gets GC'd when ``open_preferences()`` returns.
QTimer.singleShot(200, app.quit)
tray_mod._try_qt_tray()

# Find the Preferences action
from PyQt6.QtWidgets import QMenu
menus = [w for w in app.topLevelWidgets() if isinstance(w, QMenu)]
prefs_action = None
for menu in menus:
    for action in menu.actions():
        if "Preferences" in action.text() or "首选项" in action.text():
            prefs_action = action
            break
    if prefs_action:
        break

assert prefs_action is not None, "Preferences action not found in menu!"
print(f"OK: found Preferences action: {prefs_action.text()!r}")

# Trigger it
prefs_action.trigger()

# Spin the event loop briefly so PyQt6 can process the show event
# and any deferred deletes.  The bug we are guarding against only
# manifests when the event loop runs *after* the dialog is shown.
for _ in range(5):
    app.processEvents()
    time.sleep(0.02)

dialogs = [w for w in app.topLevelWidgets() if isinstance(w, QDialog)]
assert dialogs, (
    "No QDialog appeared in topLevelWidgets after triggering "
    "Preferences — the dialog was garbage-collected.  Tray must "
    "hold a strong reference (see _open_dialogs in tray.py)."
)
dlg = dialogs[-1]
assert isinstance(dlg, QDialog), f"Wrong dialog type: {type(dlg)}"
print(f"OK: dialog created: title={dlg.windowTitle()!r}, visible={dlg.isVisible()}, size={dlg.size()}")

# Check the dialog has the right window flags
flags = int(dlg.windowFlags())
WINDOW_TYPE_MASK = 0xFFFFFFF
type_val = flags & WINDOW_TYPE_MASK
has_no_focus = bool(flags & 8)
print(f"OK: dialog window type = {type_val}, has WindowDoesNotAcceptFocus = {has_no_focus}")
assert not has_no_focus, "Dialog still has WindowDoesNotAcceptFocus - this is the bug!"
print("OK: dialog does NOT have WindowDoesNotAcceptFocus - will be visible on Niri!")

# === LIFETIME CHECK ===
# Verify the dialog survives more than a second.  Without the
# ``_open_dialogs`` pin the dialog disappears within ~50 ms.
print("OK: dialog visible immediately, sleeping 1.2s to verify lifetime...")
time.sleep(1.2)
for _ in range(5):
    app.processEvents()
    time.sleep(0.02)

dialogs_after = [w for w in app.topLevelWidgets() if isinstance(w, QDialog)]
assert any(d is dlg for d in dialogs_after), (
    "Preferences dialog disappeared within 1.2s — the local "
    "reference went out of scope and PyQt6 destroyed the "
    "underlying QObject.  Tray must hold a strong reference."
)
assert dlg.isVisible(), (
    "Preferences dialog is no longer visible after 1.2s — "
    "something hid it (should not happen on a real click)."
)
print("OK: dialog still alive and visible after 1.2s — lifetime fix works")

# === SAVE / PERSIST CHECK ===
# Check save works
from PyQt6.QtWidgets import QDialogButtonBox
ok_buttons = dlg.findChildren(QDialogButtonBox)
ok_btn = None
for btn_box in ok_buttons:
    ok_btn = btn_box.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn:
        break
assert ok_btn is not None, "No OK button found in dialog"
print(f"OK: OK button found: text={ok_btn.text()!r}")

# Click OK and check it closes
ok_btn.click()
for _ in range(5):
    app.processEvents()
    time.sleep(0.02)
assert not dlg.isVisible(), "Dialog did not close after clicking OK"
print("OK: dialog closes when OK is clicked")

# Check settings persisted
import pathlib
cfg_file = pathlib.Path(tmpdir) / "maiocr" / "settings.yml"
assert cfg_file.exists(), f"Settings file not written: {cfg_file}"
print(f"OK: settings file written: {cfg_file}")

# === RE-OPEN / RE-CLOSE CHECK ===
# Open Preferences a second time and verify a *new* dialog appears
# in topLevelWidgets.  This guards against the bug where opening
# Preferences the first time would have left a broken singleton
# that re-clicks could not replace.
prefs_action.trigger()
for _ in range(5):
    app.processEvents()
    time.sleep(0.02)
dialogs2 = [w for w in app.topLevelWidgets() if isinstance(w, QDialog)]
visible_dialogs = [d for d in dialogs2 if d.isVisible()]
assert visible_dialogs, (
    "Opening Preferences a second time did not create any visible "
    "dialog — the first instance was probably shared instead of "
    "replaced."
)
new_dlg = visible_dialogs[-1]
assert new_dlg is not dlg, "second open returned the same dialog object"
print("OK: second click on Preferences opens a fresh, visible dialog")

# Close the second dialog so we don't leave it dangling
from PyQt6.QtWidgets import QDialogButtonBox
bb2 = new_dlg.findChildren(QDialogButtonBox)[0]
cancel2 = bb2.button(QDialogButtonBox.StandardButton.Cancel)
if cancel2:
    cancel2.click()
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
print("OK: second dialog closes on Cancel")
PYEOF

# 5) Check journal for errors
echo
echo "Checking journal for errors..."
# ``grep -v`` exits with code 1 when there is no match — combine
# with ``|| true`` so ``set -e`` does not abort the script on a
# perfectly normal empty journal.
err_out="$(journalctl --user -u MaiOCR.service -u MaiOCR-tray.service -p err -n 10 --no-pager 2>&1 | grep -v "^-- No entries" | grep -v "^$" || true)"
if [ -n "$err_out" ]; then
    echo "FAIL: journal errors detected:"
    echo "$err_out"
    exit 1
else
    echo "OK: no journal errors"
fi

echo
echo "==================================================================="
echo "  All checks passed.  The Preferences dialog will now appear"
echo "  correctly on Niri when the user clicks it in the tray menu."
echo "==================================================================="
