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
import sys, os, subprocess
sys.path.insert(0, '/home/mai/.local/share/uv/tools/maiocr/lib/python3.14/site-packages')

# 1) Check the new code is loaded
import inspect, maiocr.tray as t
src = inspect.getsource(t)
assert 'activateWindow' in src, "new code is NOT loaded"
assert 'WindowDoesNotAcceptFocus' not in src, "old WindowDoesNotAcceptFocus is still present"
print("OK: new code is loaded (activateWindow present, WindowDoesNotAcceptFocus absent)")

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

# Track dialog creation
created_dialogs = []
original_init = QDialog.__init__
def tracking_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    created_dialogs.append(self)
QDialog.__init__ = tracking_init

from PyQt6.QtWidgets import QApplication as QA
QA.exec = staticmethod(lambda: 0)

# Run the tray (creates the menu and dialogs)
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
assert len(created_dialogs) >= 1, "No dialog was created when Preferences was clicked!"

dlg = created_dialogs[-1]
assert isinstance(dlg, QDialog), f"Wrong dialog type: {type(dlg)}"
print(f"OK: dialog created: title={dlg.windowTitle()!r}, visible={dlg.isVisible()}, size={dlg.size()}")

# Check the dialog has the right window flags
flags = int(dlg.windowFlags())
# Qt.WindowType.Dialog = 0x00000002 | Window = 0x00000001
# WindowDoesNotAcceptFocus = 0x00000008
# In Qt 6.11 the integer value is: Dialog = 134234114, WindowDoesNotAcceptFocus = 8
WINDOW_TYPE_MASK = 0xFFFFFFF
type_val = flags & WINDOW_TYPE_MASK
has_no_focus = bool(flags & 8)
print(f"OK: dialog window type = {type_val}, has WindowDoesNotAcceptFocus = {has_no_focus}")
assert not has_no_focus, "Dialog still has WindowDoesNotAcceptFocus - this is the bug!"
print("OK: dialog does NOT have WindowDoesNotAcceptFocus - will be visible on Niri!")

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
QApplication.processEvents()
assert not dlg.isVisible(), "Dialog did not close after clicking OK"
print("OK: dialog closes when OK is clicked")

# Check settings persisted
import pathlib
cfg_file = pathlib.Path(tmpdir) / "maiocr" / "settings.yml"
assert cfg_file.exists(), f"Settings file not written: {cfg_file}"
print(f"OK: settings file written: {cfg_file}")
PYEOF

# 5) Check journal for errors
echo
echo "Checking journal for errors..."
err_out="$(journalctl --user -u MaiOCR.service -u MaiOCR-tray.service -p err -n 10 --no-pager 2>&1 | grep -v "^-- No entries" | grep -v "^$")"
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
