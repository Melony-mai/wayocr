#!/usr/bin/env bash
# Uninstall MaiOCR from this system.
#
# Stops and disables the systemd user units, removes the .desktop
# files, removes the data directory, and uninstalls the uv tool.
# Does NOT remove system packages (wl-clipboard, grim, ...) because
# the user may want to keep them for other purposes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

MAIOCRDATA_DIR="${MAIOCRDATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/maiocr}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

confirm() {
    printf '%s' "$1"
    read -r reply
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

log "MaiOCR uninstaller"
echo
if ! confirm "Remove MaiOCR systemd units, data, and CLI scripts? [y/N] "; then
    echo "Aborted."
    exit 0
fi

# Stop + disable services (best-effort)
for svc in MaiOCR-tray.service MaiOCR.service; do
    if [ -f "$HOME/.config/systemd/user/$svc" ]; then
        log "Stopping $svc"
        systemctl --user disable --now "$svc" 2>/dev/null || warn "could not stop $svc"
    fi
done

# Remove unit files
for svc in MaiOCR-tray.service MaiOCR.service; do
    rm -f "$HOME/.config/systemd/user/$svc"
    rm -f "$HOME/.config/systemd/user/default.target.wants/$svc"
done
systemctl --user daemon-reload 2>/dev/null || true

# Remove .desktop files
for name in maiocr.desktop maiocr-tray.desktop; do
    rm -f "$HOME/.local/share/applications/$name"
done
if have update-desktop-database; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

# Remove data dir
if [ -d "$MAIOCRDATA_DIR" ]; then
    log "Removing data directory $MAIOCRDATA_DIR"
    rm -rf "$MAIOCRDATA_DIR"
fi

# Uninstall uv tool
if have uv; then
    if uv tool list 2>/dev/null | grep -q '^maiocr'; then
        log "Uninstalling maiocr uv tool"
        uv tool uninstall maiocr || warn "uv tool uninstall failed"
    fi
fi

# Remove symlinks that we created
for name in maiocr maiocr-server maiocr-status maiocr-tray; do
    rm -f "$HOME/.local/bin/$name"
done

# Remove project config (settings.yml) but only if the user wants to
CONF="$HOME/.config/maiocr"
if [ -d "$CONF" ]; then
    if confirm "Remove user settings ($CONF)? [y/N] "; then
        rm -rf "$CONF"
    else
        log "Keeping $CONF"
    fi
fi

cat <<EOF

============================================================
MaiOCR has been removed from this system.
============================================================

The source checkout at ${PROJECT_DIR} is still here. You can
delete it manually if you wish.

EOF
