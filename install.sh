#!/bin/sh
# Installs gif-overlay for the current user and restarts the service if it is running.
set -e
cd "$(dirname "$0")"
data="${XDG_DATA_HOME:-$HOME/.local/share}/gif-overlay"
rm -rf "$data/gif_overlay"
mkdir -p "$data"
cp -r gif_overlay "$data/"
rm -rf "$data/gif_overlay/__pycache__"
install -Dm755 gif-overlay "$HOME/.local/bin/gif-overlay"
install -Dm644 gif-overlay.service "$HOME/.config/systemd/user/gif-overlay.service"
systemctl --user daemon-reload
if systemctl --user is-active --quiet gif-overlay; then
    systemctl --user restart gif-overlay
fi
echo "installed to $data and ~/.local/bin/gif-overlay"
