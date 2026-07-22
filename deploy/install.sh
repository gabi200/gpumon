#!/usr/bin/env bash
#
# Install gpumon as a systemd service.
#   sudo ./deploy/install.sh              install / upgrade
#   sudo ./deploy/install.sh uninstall    remove everything
#
set -euo pipefail

PREFIX=/opt/gpumon
CONFIG_DIR=/etc/gpumon
STATE_DIR=/var/lib/gpumon
UNIT=/etc/systemd/system/gpumon.service
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "This script must run as root (use sudo)." >&2
        exit 1
    fi
}

uninstall() {
    need_root
    echo "Stopping and disabling service..."
    systemctl disable --now gpumon.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    echo "Removed $PREFIX and the unit. Kept $CONFIG_DIR and $STATE_DIR."
    echo "Delete them manually if you also want the config and database gone:"
    echo "  rm -rf $CONFIG_DIR $STATE_DIR"
}

install() {
    need_root
    command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

    echo "Installing package to $PREFIX ..."
    install -d "$PREFIX"
    cp -r "$SRC_DIR/gpumon" "$PREFIX/"

    echo "Setting up config in $CONFIG_DIR ..."
    install -d "$CONFIG_DIR"
    if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
        # Ship a config whose database lives in the service StateDirectory.
        python3 - "$SRC_DIR/config.example.json" "$CONFIG_DIR/config.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
cfg = json.load(open(src))
cfg["db"] = "/var/lib/gpumon/gpumon.db"
json.dump(cfg, open(dst, "w"), indent=2)
PY
        echo "  wrote $CONFIG_DIR/config.json (edit thresholds/expected as needed)"
    else
        echo "  keeping existing $CONFIG_DIR/config.json"
    fi

    install -d "$STATE_DIR"

    echo "Installing systemd unit ..."
    cp "$SRC_DIR/deploy/gpumon.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable --now gpumon.service

    echo
    systemctl --no-pager --lines=0 status gpumon.service || true
    echo
    echo "Done. Useful commands:"
    echo "  systemctl status gpumon      # service state"
    echo "  journalctl -u gpumon -f      # live logs / alerts"
    echo "  curl localhost:8642/health   # API"
}

case "${1:-install}" in
    uninstall|remove) uninstall ;;
    install|"")       install ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
