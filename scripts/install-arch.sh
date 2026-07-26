#!/usr/bin/env bash

set -e

echo "==> Installing WayOCR for Arch Linux"

# Check Arch Linux
if ! command -v pacman >/dev/null 2>&1; then
    echo "Error: This script is for Arch Linux only."
    exit 1
fi


echo "==> Installing system dependencies"

sudo pacman -S --needed --noconfirm \
    python \
    python-pip \
    wl-clipboard \
    grim \
    slurp \
    git


# Install uv
if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv"

    curl -LsSf https://astral.sh/uv/install.sh | sh

    export PATH="$HOME/.local/bin:$PATH"
fi


echo "==> Installing WayOCR"

uv sync

uv tool install .


echo "==> Installing systemd user service"

mkdir -p ~/.config/systemd/user


cat > ~/.config/systemd/user/wayocr-server.service <<EOF
[Unit]
Description=WayOCR OCR Server

After=graphical-session.target


[Service]
Type=simple

ExecStart=%h/.local/bin/wayocr-server

Restart=on-failure
RestartSec=5


[Install]
WantedBy=default.target
EOF


echo "==> Enabling OCR server"

systemctl --user daemon-reload

systemctl --user enable --now wayocr-server


echo ""
echo "================================"
echo "WayOCR installation completed"
echo "================================"
echo ""

echo "Check status:"
echo "systemctl --user status wayocr-server"

echo ""

echo "Bind shortcut in niri:"
echo "Super+Print { spawn \"wayocr\"; }"
