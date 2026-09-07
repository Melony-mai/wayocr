#!/usr/bin/env bash
# Install / upgrade MaiOCR on Arch Linux.
#
# Idempotent. Safe to re-run as an upgrade.
#
# Steps:
#   1. Detect the OS and the calling user.
#   2. Install system packages (pacman) — wl-clipboard, grim, slurp,
#      libnotify, desktop-file-utils, cudnn, cuda, qt6-wayland, etc.
#   3. Install uv if missing.
#   4. Sync the python project (uv sync) and install the `maiocr*`
#      console scripts (uv tool install).
#   5. Install the icon and locale files into
#      ``$XDG_DATA_HOME/maiocr`` (or ``~/.local/share/maiocr``).
#   6. Install MaiOCR.service and MaiOCR-tray.service as systemd
#      user units.
#   7. Install the .desktop files for the menu / file manager.
#   8. Drop a Niri example binding next to the existing niri config.
#   9. Remove the obsolete wayocr artefacts.
#  10. Enable + start MaiOCR.service and MaiOCR-tray.service.
#  11. Print a summary of what was done and how to control the service.
#
# Exit codes:
#   0  — success
#   1  — wrong OS
#   2  — user declined sudo
#   3  — required system package failed to install
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Allow overriding with a custom data dir for tests
MAIOCRDATA_DIR="${MAIOCRDATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/maiocr}"

have() { command -v "$1" >/dev/null 2>&1; }

# sudo-or-fail. Some users run the script as root (e.g. inside a
# container); detect that and skip the sudo wrapper.
maybe_sudo() {
    if [ "$(id -u)" = "0" ]; then
        "$@"
    else
        if ! have sudo; then
            err "sudo is required to install system packages"
            exit 2
        fi
        sudo "$@"
    fi
}

# ---------------------------------------------------------------------------
# 1. OS detection
# ---------------------------------------------------------------------------

log "Detecting environment"
if ! have pacman; then
    err "This installer targets Arch Linux (no pacman found)."
    err "Please install MaiOCR manually on your distribution."
    exit 1
fi

# Detect NVIDIA
HAS_NVIDIA=0
if ls /dev/nvidia0 >/dev/null 2>&1; then
    HAS_NVIDIA=1
fi
log "NVIDIA GPU detected: $([ "$HAS_NVIDIA" = "1" ] && echo yes || echo no)"

# ---------------------------------------------------------------------------
# 2. Install system packages
# ---------------------------------------------------------------------------

SYSTEM_PKGS=(
    python
    wl-clipboard
    grim
    slurp
    libnotify
    desktop-file-utils
    xdg-utils
)

# Qt is only required for the system-tray icon; headless installs
# (e.g. a server) skip these.
QT_PKGS=(
    qt6-base
    qt6-wayland
    python-pyqt6
)

# CUDA/cuDNN only when an NVIDIA device is present.
GPU_PKGS=()
if [ "$HAS_NVIDIA" = "1" ]; then
    GPU_PKGS=(cudnn cuda)
fi

log "Installing system packages"
if ! maybe_sudo pacman -S --needed --noconfirm "${SYSTEM_PKGS[@]}"; then
    err "Failed to install required system packages"
    exit 3
fi

# Optional but recommended packages
for grp in QT_PKGS GPU_PKGS; do
    case "$grp" in
        QT_PKGS)  pkgs=("${QT_PKGS[@]}") ;;
        GPU_PKGS) pkgs=("${GPU_PKGS[@]}") ;;
    esac
    [ "${#pkgs[@]}" -eq 0 ] && continue
    if ! maybe_sudo pacman -S --needed --noconfirm "${pkgs[@]}" 2>/dev/null; then
        warn "Some optional packages failed to install ($grp) — MaiOCR will still work, with reduced functionality."
    fi
done

# ---------------------------------------------------------------------------
# 3. Install uv
# ---------------------------------------------------------------------------

if ! have uv; then
    log "Installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ---------------------------------------------------------------------------
# 4. Sync the project and install the CLI scripts
# ---------------------------------------------------------------------------

cd "${PROJECT_DIR}"

# Prefer the system Python so the Arch-packaged python-pyqt6 lines up
# with the tool's interpreter. uv picks a python per the project's
# .python-version when one exists.
SYSTEM_PY="$(command -v python3 || true)"
if [ -n "${SYSTEM_PY}" ]; then
    log "Using system Python: ${SYSTEM_PY}"
    export UV_PYTHON="${SYSTEM_PY}"
fi

log "Syncing python project (uv sync)"
uv sync --extra dev

# Detect the python version that uv created
PY_VER="$(.venv/bin/python -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}")' 2>/dev/null || true)"
if [ -z "${PY_VER}" ]; then
    PY_VER="$(ls -1 .venv/bin/python* 2>/dev/null | head -1 | sed 's@.*/python@@')"
fi
# Strip non-numeric prefix (e.g. "3.14" -> "3.14", "3" -> "3")
PY_VER="$(echo "${PY_VER}" | grep -oE '^[0-9]+\.[0-9]+' || echo "${PY_VER}")"
log "Detected python ${PY_VER}"

# Make the system Qt6 / PyQt6 available to the uv tool's venv.
# The Arch package ``python-pyqt6`` installs into the system Python's
# site-packages, but ``uv tool install`` creates a private venv. We
# link the relevant files into the tool's site-packages so the tray
# icon works.
SYSTEM_SITE=""
for candidate in \
    "/usr/lib/python${PY_VER}/site-packages" \
    "$(python3 -c 'import sysconfig, os; print(sysconfig.get_paths("purelib"))' 2>/dev/null)"
do
    if [ -d "${candidate}" ] && [ -d "${candidate}/PyQt6" ]; then
        SYSTEM_SITE="${candidate}"
        break
    fi
done

if have maiocr-server && [ -n "${SYSTEM_SITE}" ]; then
    TOOL_PY="$HOME/.local/share/uv/tools/maiocr/bin/python"
    SITE_BASE="$HOME/.local/share/uv/tools/maiocr/lib/python${PY_VER}/site-packages"
    if [ -x "${TOOL_PY}" ] && [ -d "${SITE_BASE}" ]; then
        log "Linking system PyQt6 into the maiocr tool venv"
        # The system site may split a single PyQt6 across multiple
        # .dist-info dirs; copy/symlink each top-level package.
        for pkg_dir in "${SYSTEM_SITE}"/PyQt6*; do
            [ -d "$pkg_dir" ] || continue
            name="$(basename "$pkg_dir")"
            if [ ! -e "${SITE_BASE}/${name}" ]; then
                ln -sfn "$pkg_dir" "${SITE_BASE}/${name}" 2>/dev/null || true
            fi
        done
        # Copy .dist-info for importlib.metadata
        for info in "${SYSTEM_SITE}"/PyQt6*.dist-info; do
            [ -d "$info" ] || continue
            cp -r --no-preserve=ownership "$info" "${SITE_BASE}/" 2>/dev/null || true
        done
        if "${TOOL_PY}" -c "import PyQt6.QtWidgets" 2>/dev/null; then
            log "PyQt6 available in the maiocr tool venv"
        else
            warn "PyQt6 could not be linked; tray will run headless"
        fi
    fi
fi

# uv tool install: this makes the `maiocr*` commands available in
# ``$HOME/.local/bin`` (no venv activation required).  We pin the
# Python to the system interpreter so the system-packaged PyQt6
# is importable from the tool venv.
log "Installing maiocr console scripts"
if have maiocr; then
    uv tool uninstall maiocr >/dev/null 2>&1 || true
fi
TOOL_PY_ARG=()
if [ -n "${SYSTEM_PY:-}" ]; then
    TOOL_PY_ARG=(--python "${SYSTEM_PY}")
fi
uv tool install --force "${TOOL_PY_ARG[@]}" .

# Re-link PyQt6 because `uv tool install --force` recreates the venv.
if have maiocr-server && [ -n "${SYSTEM_SITE}" ]; then
    TOOL_PY="$HOME/.local/share/uv/tools/maiocr/bin/python"
    SITE_BASE="$HOME/.local/share/uv/tools/maiocr/lib/python${PY_VER}/site-packages"
    if [ -x "${TOOL_PY}" ] && [ -d "${SITE_BASE}" ]; then
        log "Re-linking system PyQt6 into the new tool venv"
        for pkg_dir in "${SYSTEM_SITE}"/PyQt6*; do
            [ -d "$pkg_dir" ] || continue
            name="$(basename "$pkg_dir")"
            if [ ! -e "${SITE_BASE}/${name}" ]; then
                ln -sfn "$pkg_dir" "${SITE_BASE}/${name}" 2>/dev/null || true
            fi
        done
        for info in "${SYSTEM_SITE}"/PyQt6*.dist-info; do
            [ -d "$info" ] || continue
            cp -r --no-preserve=ownership "$info" "${SITE_BASE}/" 2>/dev/null || true
        done
    fi
fi

# Make sure `maiocr cleanup` (called below) picks up the new install
export PATH="$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# 5. Install icon, locales and other data files
# ---------------------------------------------------------------------------

log "Installing data files into ${MAIOCRDATA_DIR}"
mkdir -p "${MAIOCRDATA_DIR}"

ICON_SRC="${PROJECT_DIR}/icon.ico"
if [ -f "${ICON_SRC}" ]; then
    install -m 0644 "${ICON_SRC}" "${MAIOCRDATA_DIR}/icon.ico"
else
    warn "icon.ico not found in ${PROJECT_DIR}; tray will use a generated icon"
fi

# Locales + bundled data — copy from the source so the data dir
# is portable even if the user does not have the python package
# installed in their PYTHONPATH.
LOCALES_SRC="${PROJECT_DIR}/src/maiocr/locales"
if [ -d "${LOCALES_SRC}" ]; then
    mkdir -p "${MAIOCRDATA_DIR}/locales"
    cp -f "${LOCALES_SRC}"/*.yml "${MAIOCRDATA_DIR}/locales/" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 6. Install systemd user units
# ---------------------------------------------------------------------------

log "Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "${PROJECT_DIR}/scripts/MaiOCR.service" \
    "$HOME/.config/systemd/user/MaiOCR.service"
install -m 0644 "${PROJECT_DIR}/scripts/MaiOCR-tray.service" \
    "$HOME/.config/systemd/user/MaiOCR-tray.service"

systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7. Install .desktop files
# ---------------------------------------------------------------------------

log "Installing desktop entries"
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "${APPS_DIR}"
for desk in "${PROJECT_DIR}/scripts"/*.desktop; do
    [ -f "$desk" ] || continue
    install -m 0644 "$desk" "${APPS_DIR}/$(basename "$desk")"
done
# Refresh the desktop database if the tool is installed
if have update-desktop-database; then
    update-desktop-database "${APPS_DIR}" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 8. Drop a Niri example binding (do not clobber user config)
# ---------------------------------------------------------------------------

NIRI_DMS_DIR="$HOME/.config/niri/dms"
if [ -d "$NIRI_DMS_DIR" ] || [ -d "$HOME/.config/niri" ]; then
    log "Niri config detected — example MaiOCR bindings written to scripts/maiocr.niri.kdl"
    log "Add to your niri config: include \"maiocr.niri.kdl\"  (path may need adjustment)"
fi

# ---------------------------------------------------------------------------
# 9. Clean up old wayocr artefacts
# ---------------------------------------------------------------------------

log "Removing obsolete wayocr-server artefacts"
maiocr cleanup 2>/dev/null || true

# ---------------------------------------------------------------------------
# 10. Enable + start the services
# ---------------------------------------------------------------------------

log "Enabling and starting MaiOCR.service"
systemctl --user enable --now MaiOCR.service

# Tray service is optional (no PyQt6 -> no tray). We start it best-
# effort and warn if it cannot run.
if have maiocr-tray; then
    if systemctl --user enable --now MaiOCR-tray.service 2>/dev/null; then
        log "MaiOCR-tray.service enabled and started"
    else
        warn "MaiOCR-tray.service failed to start (PyQt6 missing?)"
        warn "You can still use MaiOCR from the command line: maiocr"
    fi
else
    warn "maiocr-tray not on PATH; skipping tray autostart"
fi

# ---------------------------------------------------------------------------
# 11. Post-install checks
# ---------------------------------------------------------------------------

log "Post-install checks"
sleep 1
if systemctl --user is-active --quiet MaiOCR.service; then
    log "MaiOCR.service is active"
else
    warn "MaiOCR.service is not active; check 'journalctl --user -u MaiOCR.service -n 30'"
fi

if [ "$HAS_NVIDIA" = "1" ]; then
    if have nvidia-smi; then
        if LD_LIBRARY_PATH=/opt/cuda/lib64:/usr/lib maiocr ping >/dev/null 2>&1; then
            log "MaiOCR server is reachable"
        else
            warn "MaiOCR server did not respond to ping"
        fi
        log "GPU info:"
        LD_LIBRARY_PATH=/opt/cuda/lib64:/usr/lib maiocr gpu-info || true
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

cat <<EOF

============================================================
MaiOCR installation complete
============================================================

Manage MaiOCR:
  systemctl --user status  MaiOCR.service
  systemctl --user restart MaiOCR.service
  systemctl --user stop    MaiOCR.service
  systemctl --user disable MaiOCR.service
  systemctl --user status  MaiOCR-tray.service

CLI helpers:
  maiocr                # screenshot -> OCR -> clipboard
  maiocr release-vram   # drop the OCR engine, free VRAM
  maiocr status
  maiocr gpu-info
  maiocr restart
  maiocr logs -f
  maiocr settings show
  maiocr settings lang  zh-CN   # switch to Chinese
  maiocr settings lang  en      # switch to English
  maiocr tray           # start the system tray manually

Bind shortcut in niri (config.kdl):
  Mod+Print { spawn "maiocr"; }

Data files:    ${MAIOCRDATA_DIR}
Service unit:  ~/.config/systemd/user/MaiOCR.service
Tray unit:     ~/.config/systemd/user/MaiOCR-tray.service

EOF
