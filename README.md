# MaiOCR

> GPU-accelerated screenshot OCR service for Wayland — with a
> system-tray indicator, persistent settings, and English / Chinese
> interface.

MaiOCR lets you grab a screen area, recognise the text inside it
(Chinese, English, Japanese, mixed), and copy the result to the
clipboard. The OCR model runs on your NVIDIA GPU when one is
available, with a CPU fallback.

The whole thing lives in the background: a `systemd` user service
holds the model, a separate tray process shows the status icon and
context menu, and your key binding triggers an OCR request through a
Unix-domain socket.

## Highlights

- **NVIDIA GPU acceleration** — `onnxruntime-gpu` with the CUDA
  execution provider, auto-detected at start-up. CPU fallback when no
  compatible GPU is found.
- **Lazy VRAM** — the OCR model is loaded into VRAM only when an OCR
  request actually arrives, and can be released on demand without
  stopping the service. A configurable auto-release timer is also
  available.
- **Persistent server** — `systemd --user` service keeps the model
  warm between OCR calls.
- **Layout-preserving post-processing** — the recogniser returns
  bounding boxes that the post-processor uses to preserve indentation,
  paragraph breaks, code alignment, columns, and reading order.
- **Mixed-language OCR** — Chinese (Simplified / Traditional),
  Japanese, English, and mixed.
- **System-tray indicator** — a Niri / DankMaterialShell-friendly
  SNI tray icon with right-click actions (Release VRAM, Restart,
  Start, Stop, Service status, GPU info, Run OCR, Preferences, Quit).
  Left-click and double-click actions are configurable from
  Preferences.
- **Live status file** at `$XDG_RUNTIME_DIR/maiocr/status.json` that
  DMS Quickshell widgets can poll.
- **English / 简体中文 UI** — switch at any time with
  `maiocr settings lang zh-CN`. The choice is persisted in
  `$XDG_CONFIG_HOME/maiocr/settings.yml`.
- **Arch-Linux-first** — single install script installs system
  packages, the GPU runtime, the systemd units, the desktop entries,
  and the tray autostart.

## Supported OCR languages

| Language        | Notes                          |
|-----------------|--------------------------------|
| English         | default                        |
| Simplified Chinese (简体中文) | default model     |
| Traditional Chinese | supported via the default model |
| Japanese        | supported via the default model |
| Mixed scripts   | works out of the box           |

The bundled model is the [RapidOCR](https://github.com/RapidAI/RapidOCR)
PP-OCRv6 small variant, which is small enough to fit on a single
8 GB consumer GPU and fast enough to keep the workflow snappy. The
model is downloaded automatically the first time the OCR engine is
built.

## System requirements

- Arch Linux (the installer uses `pacman`). Other distributions can
  install the Python package from source — see
  [Development setup](#development-setup) below.
- A Wayland session. Tested on niri; should work on any compositor
  that exposes `WAYLAND_DISPLAY` (Sway, Hyprland, …).
- Python 3.11 or newer.
- Optional: an NVIDIA GPU with proprietary driver (>= 535). When
  present, the installer adds `cudnn` and `cuda` for you.

Required tools (installed automatically):

| Tool          | Used for                |
|---------------|-------------------------|
| `wl-clipboard`| copy OCR result         |
| `grim`        | capture the screenshot  |
| `slurp`       | select the area         |
| `libnotify`   | desktop notifications   |

Optional, but recommended for the tray icon:

| Tool            | Used for                                                  |
|-----------------|-----------------------------------------------------------|
| `qt6-base`      | Qt 6 runtime                                              |
| `qt6-wayland`   | Qt-on-Wayland plugin                                      |
| `python-pyqt6`  | Python bindings for Qt6 (must match the tool's Python)     |
| `python-dbus`   | Optional. Enables the D-Bus notification backend fallback. |

## Installation

Clone the repository and run the install script:

```bash
git clone https://github.com/Melony-mai/maiocr.git
cd maiocr
./scripts/install-arch.sh
```

The script is **idempotent** — re-run it to upgrade. It will:

1. Verify the host is Arch Linux.
2. Install the system packages via `pacman`
   (`wl-clipboard`, `grim`, `slurp`, `libnotify`,
   `desktop-file-utils`, `xdg-utils`, plus `qt6-*` and the NVIDIA
   CUDA / cuDNN packages if a compatible GPU is detected).
3. Install [`uv`](https://docs.astral.sh/uv/) if it is not already on
   the `PATH`.
4. `uv sync` the project and install the `maiocr*` console scripts
   with `uv tool install --force`.
5. Copy `icon.ico` and the locale files into
   `${XDG_DATA_HOME:-$HOME/.local/share}/maiocr/`.
6. Install `MaiOCR.service` and `MaiOCR-tray.service` under
   `~/.config/systemd/user/`.
7. Install `.desktop` entries under
   `~/.local/share/applications/`.
8. Drop a Niri example binding into `scripts/maiocr.niri.kdl`.
9. Remove obsolete `wayocr-server.service` artefacts.
10. Enable and start the `MaiOCR.service` and `MaiOCR-tray.service`
    user units.
11. Print a summary of useful commands and the data / config paths.

## First-time setup

1. **Add a Niri key binding** (one-time).

   In `~/.config/niri/config.kdl` (or the file your `include`s
   reference):

   ```kdl
   Mod+Print { spawn "maiocr"; }
   ```

   Or include the example file shipped in this repo:

   ```kdl
   include "~/.local/share/maiocr/maiocr.niri.kdl"
   ```

2. **Pick a UI language**:

   ```bash
   maiocr settings lang en      # English
   maiocr settings lang zh-CN   # 简体中文
   ```

3. **Try it**: press <kbd>Super</kbd> + <kbd>Print</kbd>, select an
   area, and watch the result land in your clipboard. A desktop
   notification will tell you how many characters were recognised.

## How to use MaiOCR

### Trigger an OCR

| Method                          | What it does                              |
|---------------------------------|-------------------------------------------|
| <kbd>Super</kbd>+<kbd>Print</kbd> | screenshot → OCR → clipboard            |
| `maiocr`                        | same, from a terminal                     |
| `maiocr run`                    | same                                      |

### Manage the service

```bash
systemctl --user status  MaiOCR.service
systemctl --user restart MaiOCR.service
systemctl --user stop    MaiOCR.service
systemctl --user disable MaiOCR.service
```

CLI shortcuts:

```bash
maiocr start                  # start (or restart) the server and tray
maiocr stop                   # stop the server and tray
maiocr restart                # restart the server and tray
maiocr status
maiocr ping
maiocr logs -f
maiocr refresh-tray           # restart just the tray (picks up new binary)
maiocr repair                 # re-link system PyQt6 + restart the tray
```

### Release VRAM

```bash
maiocr release-vram
```

The model stays out of VRAM until the next OCR call. The status file
at `$XDG_RUNTIME_DIR/maiocr/status.json` and the tray icon both
update within a couple of seconds.

### Configure

```bash
maiocr settings show                              # show all settings
maiocr settings lang zh-CN                        # switch to Chinese
maiocr settings lang en                           # back to English
maiocr settings set auto_release_vram=true        # release after every OCR
maiocr settings set auto_release_seconds=120      # release after 2 min idle
maiocr settings set prefer_gpu=false              # CPU-only
maiocr settings set notify_on_copy=false          # silence the popup
maiocr settings set log_level=debug               # verbose logs
```

All settings are persisted in
`$XDG_CONFIG_HOME/maiocr/settings.yml`.

The same settings are editable from the **Preferences** entry in the
tray's right-click menu.

### Update MaiOCR

```bash
cd ~/path/to/maiocr
git pull
./scripts/install-arch.sh
```

The install script detects existing services and restarts them only
when needed.

## Tray icon

`MaiOCR-tray.service` launches `maiocr-tray` in the background. The
icon shows the current state via a coloured dot:

| Colour | Meaning                          |
|--------|----------------------------------|
| green  | engine loaded on GPU             |
| blue   | engine loaded on CPU             |
| red    | service not running              |
| yellow | unknown / transitional state     |

Right-click menu:

- **Run OCR screenshot** (spawns the capture dialog)
- **Release VRAM**
- **Restart MaiOCR**
- **Start MaiOCR**
- **Stop MaiOCR**
- **Service status**
- **GPU info**
- **Preferences…** (open the settings dialog)
- **Quit**

The tray reads the icon from the system data directory and falls back
to a generated icon if the file is missing.

## Notifications

MaiOCR has a clean event / backend / manager architecture.  Every
notification is built as a logical ``Event`` (id, kind, title,
body) and the **single** active backend is responsible for rendering
it.  The two notification modes are mutually exclusive: the
**non-active** backends are never invoked.

| Mode                       | What you see                                                                                  |
|----------------------------|-----------------------------------------------------------------------------------------------|
| **Standard Notification** (D-Bus, default) | A native desktop notification via the freedesktop notification spec over D-Bus.  Rendered by the user's notification daemon (mako, dunst, fnott, the dms / quickshell built-in notifier, etc.). |
| **System Notification**    | A native desktop notification via `notify-send`.  The fallback if D-Bus is not reachable.  |

The previous "Custom Notification Bubble" mode has been folded into
the Standard Notification mode: a "custom" notification on Niri is
just a well-formed D-Bus notification rendered by the user's
notification daemon.  ``notification_mode`` accepts ``"dbus"``
(alias: ``"custom"``) and ``"system"``; both are non-focus-stealing
and never create a MaiOCR Wayland surface.

### Why MaiOCR never opens a notification window

The freedesktop notification spec is deliberately non-focus-stealing:
the bubble is drawn by your notification daemon as a layer-shell
overlay, so MaiOCR itself never creates a new Wayland toplevel
window for a notification (a new ``wl_shell_surface`` would make
Niri move focus to it, which is disruptive).

For the same reason the tray icon is a ``QSystemTrayIcon`` (the SNI
protocol — the SNI host draws the icon in its reserved area, not as
a MaiOCR window) and capture uses ``slurp`` + ``grim`` (which use
the layer-shell protocol themselves for the region-selection
overlay).  This design is the most portable across sway, hyprland,
river and other Wayland compositors.

### Notification duration

Configure the bubble display time in **Preferences → Bubble
display duration** (or via the CLI:

```bash
maiocr settings set notification_duration=8
```

The value is in seconds (1 – 60) and is persisted in
``settings.yml``.  Silent task-completion indicators are
transient (do not enter the persistent notification history)
regardless of this setting.

### Service Status / GPU Information / Preferences dialogs

All three dialogs opened from the tray are normal QDialog windows.
The user has explicitly clicked an entry in the tray menu, so
taking focus is the expected behaviour.  They use the standard
QDialog window flags and ``dlg.show() + dlg.raise_() +
dlg.activateWindow()`` to surface reliably on Niri, sway, KDE
Plasma and X11.  The dialogs are non-modal (``dlg.show()`` rather
than ``dlg.exec()``) so the tray can keep updating the status
icon while a dialog is open.  Each open dialog is also held in
the tray's ``_open_dialogs`` list so the Python wrapper is not
garbage-collected (which would otherwise destroy the underlying
QObject and make the window vanish within milliseconds).

Notifications, which fire automatically and have *not* been
requested by the user, are still routed through the freedesktop
notification spec over D-Bus so they never steal focus.

### Click actions

Left-click and double-click on the tray icon are independently
configurable from Preferences. Available actions:

| ID             | Action                                  |
|----------------|-----------------------------------------|
| `none`         | Do nothing                              |
| `release_vram` | Drop the OCR engine, free VRAM          |
| `run_ocr`      | Open the OCR screenshot capture dialog  |
| `show_status`  | Show the Service status dialog         |
| `show_gpu`     | Show the GPU information dialog         |
| `restart`      | Restart MaiOCR (server + tray)          |
| `preferences`  | Open the preferences dialog             |
| `quit`         | Exit the tray process                   |

Defaults: left-click = `release_vram`, double-click = `run_ocr`.
Single-click and double-click are disambiguated by a 250 ms
timeout — a true double-click does not also fire the single-click
action.

Change the action from the CLI:

```bash
maiocr settings set click_action_left=run_ocr
maiocr settings set click_action_double=release_vram
```

The setting is persisted in `settings.yml` and takes effect the
next time the tray reads it (no restart required — the tray
re-reads the value on every click).

## File locations

| What                          | Path                                                      |
|-------------------------------|-----------------------------------------------------------|
| Python package                | `~/.local/share/uv/tools/maiocr/`                         |
| Console scripts               | `~/.local/bin/maiocr*`                                    |
| System data (icon, locales)   | `${XDG_DATA_HOME:-$HOME/.local/share}/maiocr/`            |
| User settings                 | `${XDG_CONFIG_HOME:-$HOME/.config}/maiocr/settings.yml`   |
| Systemd user units            | `~/.config/systemd/user/MaiOCR*.service`                  |
| Desktop entries               | `~/.local/share/applications/maiocr*.desktop`             |
| Runtime socket                | `$XDG_RUNTIME_DIR/maiocr/maiocr.sock`                     |
| Runtime status file           | `$XDG_RUNTIME_DIR/maiocr/status.json`                     |
| Server log                    | `$XDG_RUNTIME_DIR/maiocr/server.log`                      |

## Troubleshooting

### The service is installed but `maiocr status` says it is inactive

```bash
journalctl --user -u MaiOCR.service -n 50
```

Common causes:

- `LD_LIBRARY_PATH` is missing — the install script adds
  `/opt/cuda/lib64:/usr/lib` to the unit file; check that those
  directories exist.
- `python-pyqt6` was not installed; the tray is harmless but the
  service can still fail to import `onnxruntime-gpu` on systems
  without cuDNN.

### The tray icon does not appear

```bash
journalctl --user -u MaiOCR-tray.service -n 30
```

If the log says `PyQt6 unavailable:`, install `python-pyqt6` for the
same Python version that the tool uses:

```bash
pacman -S python-pyqt6
# rerun the installer to re-link PyQt6 into the tool venv
./scripts/install-arch.sh
```

You can also force a re-link with the new `maiocr repair` command:

```bash
maiocr repair
```

### Notifications do not appear

MaiOCR delivers notifications through the freedesktop notification
spec over D-Bus (``org.freedesktop.Notifications``), rendered by
your notification daemon (mako, dunst, fnott, the dms / quickshell
built-in notifier, …).  If a notification is missing:

1. Confirm a notification daemon is actually running on your
   session.  On a bare Niri setup the installer installs ``mako``
   for you:
   ```bash
   pgrep -x mako || pgrep -x dunst || pgrep -x fnott
   ```
2. Verify the D-Bus backend is selected and reachable:
   ```bash
   maiocr settings show | grep notification_mode
   # expected: "system" or "custom" (an alias for "dbus")
   ```
   The tray logs which backend it picked at start-up:
   ```bash
   journalctl --user -u MaiOCR-tray.service -n 30 | grep -i "backend\|notification"
   ```
   You should see ``active=dbus`` when the D-Bus Notifications
   service is available.
3. If the mode is ``system``, check that ``notify-send`` exists
   (``libnotify`` is installed by the installer).
4. The D-Bus notification spec never creates a MaiOCR window, so
   there is no MaiOCR Wayland surface to hide — if the daemon is
   running and the backend is active, the bubble appears from your
   daemon.  Check the daemon's own logs if it still does not show.

Notifications triggered from a terminal (``maiocr run``,
``maiocr start``, …) are dropped as a request file for the tray
process to forward to the backend; this can add up to ~2 s of
latency (one tray refresh cycle).

### `maiocr gpu-info` reports CPU only

- Make sure the proprietary NVIDIA driver is loaded:
  `nvidia-smi` should print your GPU.
- Install `cudnn` and `cuda`:
  `sudo pacman -S cudnn cuda`
- Restart the service: `systemctl --user restart MaiOCR.service`.

### OCR returns no text

- The image is too small or the contrast is too low. The preprocessor
  applies CLAHE and an invert for dark backgrounds; very low-contrast
  images may still fail.
- Set the log level to `debug` to see what the engine sees:
  `maiocr settings set log_level=debug && maiocr restart`.

## Uninstallation

```bash
./scripts/uninstall-arch.sh
```

The script:

- Stops and disables the systemd user units.
- Removes the unit files and their `default.target.wants` symlinks.
- Removes the `.desktop` entries.
- Removes `${XDG_DATA_HOME:-$HOME/.local/share}/maiocr/`.
- Uninstalls the `maiocr` uv tool.
- Removes the `maiocr*` symlinks under `~/.local/bin/`.
- Asks before deleting user settings.

System packages (`wl-clipboard`, `cudnn`, …) are left in place.

## Development setup

```bash
git clone https://github.com/Melony-mai/maiocr.git
cd maiocr
uv sync --extra dev            # creates ./.venv
.venv/bin/python -m pytest     # 120+ tests
```

The Python package lives in `src/maiocr/`. Entry points:

| Console script   | Module                          |
|------------------|---------------------------------|
| `maiocr`         | `maiocr.cli:main`               |
| `maiocr-server`  | `maiocr.server:main`            |
| `maiocr-status`  | `maiocr.status:main`            |
| `maiocr-tray`    | `maiocr.tray:main`              |
| `wayocr*`        | `maiocr.compat*` (legacy shims) |

### Project layout

```
maiocr
├── pyproject.toml
├── icon.ico                        # the application icon
├── README.md
├── scripts/
│   ├── install-arch.sh             # one-shot installer
│   ├── uninstall-arch.sh           # removes the install
│   ├── MaiOCR.service              # systemd user unit (server)
│   ├── MaiOCR-tray.service         # systemd user unit (tray)
│   ├── maiocr.desktop              # .desktop entry
│   ├── maiocr-tray.desktop         # .desktop entry for the tray
│   └── maiocr.niri.kdl             # example niri binding
├── src/maiocr/
│   ├── __init__.py
│   ├── cli.py                      # `maiocr` command
│   ├── server.py                   # `maiocr-server`
│   ├── client.py                   # socket client
│   ├── status.py                   # `maiocr-status`
│   ├── tray.py                     # `maiocr-tray`
│   ├── ocr.py                      # RapidOCR wrapper (lazy, GPU, release)
│   ├── gpu.py                      # GPU detection
│   ├── preprocess.py               # image preprocessing
│   ├── processor.py                # layout-preserving formatter
│   ├── capture.py                  # grim+slurp screenshot
│   ├── clipboard.py                # wl-copy
│   ├── notify.py                   # notify-send
│   ├── paths.py                    # XDG runtime paths
│   ├── resources.py                # icon discovery
│   ├── i18n.py                     # i18n dispatcher
│   ├── settings.py                 # user settings
│   ├── cleanup.py                  # removes old wayocr artefacts
│   ├── compat*.py                  # legacy wayocr* command shims
│   ├── data/icon.ico               # packaged icon
│   └── locales/                    # bundled translations
│       ├── en.yml
│       └── zh-CN.yml
└── tests/
    ├── conftest.py
    ├── test_cleanup.py
    ├── test_client_server.py
    ├── test_cli.py
    ├── test_gpu.py
    ├── test_i18n.py
    ├── test_ocr.py
    ├── test_preprocess.py
    ├── test_processor.py
    ├── test_resources.py
    ├── test_settings.py
    └── test_tray.py
```

## License

MIT
