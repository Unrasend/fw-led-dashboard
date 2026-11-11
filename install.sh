#!/usr/bin/env bash
# Install, update or remove the Framework LED Matrix dashboard for the current user.
#
#   ./install.sh              install or update (safe to re-run after pulling new code)
#   ./install.sh --uninstall  stop and remove the service, tray entry and venv; keep the config
#
# Installs:
#   ~/.local/share/framework-led-dashboard/venv   venv reusing system pyserial/PyGObject/pycairo
#   ~/.config/framework-led-dashboard/config.toml created once, with both panels detected
#   ~/.config/systemd/user/framework-led-dashboard.service   enabled and (re)started
#   ~/.config/autostart/framework-led-dashboard-tray.desktop  tray icon at login
# An older inputmodule-rs visualizer service (framework-matrix.service), if present, is
# disabled - never deleted - because only one program can own the panels.
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA_DIR="$HOME/.local/share/framework-led-dashboard"
CONFIG_DIR="$HOME/.config/framework-led-dashboard"
STATE_DIR="$HOME/.local/state/framework-led-dashboard"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_FILE="$HOME/.config/autostart/framework-led-dashboard-tray.desktop"
VENV="$DATA_DIR/venv"
BIN="$VENV/bin/framework-led-dashboard"
SERVICE=framework-led-dashboard.service
OLD_SERVICE=framework-matrix.service
PYTHON=${PYTHON:-python3}

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

packages_hint() {
    cat >&2 <<'EOF'
Install the system packages first, e.g.:
  Arch / Manjaro:  sudo pacman -S python python-pyserial python-gobject python-cairo gtk3 libpulse
  Debian / Ubuntu: sudo apt install python3 python3-venv python3-serial python3-gi python3-gi-cairo gir1.2-gtk-3.0 pulseaudio-utils
  Fedora:          sudo dnf install python3 python3-pyserial python3-gobject python3-cairo gtk3 pulseaudio-utils
EOF
}

stop_tray() {
    pkill -f "$BIN --tray" 2>/dev/null || true
}

uninstall() {
    step "Removing $SERVICE"
    systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    systemctl --user daemon-reload
    step "Removing the tray"
    stop_tray
    rm -f "$AUTOSTART_FILE"
    step "Removing $DATA_DIR"
    rm -rf "$VENV"
    rmdir "$DATA_DIR" 2>/dev/null || true
    printf '\nKept your settings: %s and %s (delete them to remove everything).\n' \
        "$CONFIG_DIR" "$STATE_DIR"
    if systemctl --user cat "$OLD_SERVICE" >/dev/null 2>&1; then
        printf 'The old visualizer is still installed: systemctl --user enable --now %s\n' \
            "$OLD_SERVICE"
    fi
}

case "${1:-}" in
    "") ;;
    --uninstall) uninstall; exit 0 ;;
    -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "unknown option: $1 (use --uninstall or --help)" ;;
esac

step "Checking requirements"
command -v systemctl >/dev/null || fail "systemd is required"
command -v "$PYTHON" >/dev/null || { packages_hint; fail "$PYTHON not found"; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || fail "$PYTHON is older than 3.11"
"$PYTHON" -c 'import venv, serial' 2>/dev/null || { packages_hint; fail "python venv or pyserial is missing"; }
command -v parec >/dev/null \
    || printf 'warning: parec not found - the equalizer stays dark (install libpulse / pulseaudio-utils)\n' >&2

step "Installing the package into $VENV"
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
    --no-build-isolation --no-deps --upgrade "$PROJECT_DIR"
"$VENV/bin/python" -c 'import serial, framework_led_dashboard' \
    || fail "the installed package cannot be imported"
if ! "$VENV/bin/python" -c 'import cairo, gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    printf 'warning: GTK 3 / PyGObject / pycairo missing - the tray icon will not start\n' >&2
    packages_hint
fi

step "Configuration in $CONFIG_DIR"
if [[ -f "$CONFIG_DIR/config.toml" ]]; then
    printf 'keeping the existing config.toml\n'
elif [[ -f "$PROJECT_DIR/config.toml" ]]; then
    install -Dm644 "$PROJECT_DIR/config.toml" "$CONFIG_DIR/config.toml"
    printf 'copied %s\n' "$PROJECT_DIR/config.toml"
else
    "$BIN" --init-config "$PROJECT_DIR/config.example.toml" "$CONFIG_DIR/config.toml" \
        || fail "could not detect both panels; see '$BIN --list-panels'"
fi
"$BIN" --check-config "$CONFIG_DIR/config.toml" >/dev/null \
    || fail "$CONFIG_DIR/config.toml is invalid"

step "Checking that no hand-started dashboard holds the panels"
if ! systemctl --user --quiet is-active "$SERVICE" \
    && "$VENV/bin/python" -c 'from framework_led_dashboard.control import ControlClient; ControlClient().status()' 2>/dev/null; then
    fail "a dashboard started by hand is running; stop it (Ctrl+C or kill) and re-run"
fi

if systemctl --user cat "$OLD_SERVICE" >/dev/null 2>&1; then
    step "Disabling the old visualizer $OLD_SERVICE (kept for rollback)"
    systemctl --user disable --now framework-matrix.service
fi

step "Installing and starting $SERVICE"
install -Dm644 "$PROJECT_DIR/packaging/framework-led-dashboard.service" "$UNIT_DIR/$SERVICE"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE"
systemctl --user restart "$SERVICE"

step "Tray icon at login"
mkdir -p "$(dirname "$AUTOSTART_FILE")"
sed "s|@BIN@|$BIN|" "$PROJECT_DIR/packaging/framework-led-dashboard-tray.desktop" > "$AUTOSTART_FILE"
if [[ -n "${DISPLAY:-}" ]]; then
    stop_tray
    setsid -f "$BIN" --tray >/dev/null 2>&1
    printf 'tray (re)started\n'
else
    printf 'no X display in this shell: the tray starts at the next login\n'
fi

step "Status"
sleep 2
systemctl --user --no-pager --lines=8 status "$SERVICE" || true
cat <<EOF

Done. Logs:     journalctl --user -u $SERVICE -f
      Config:   $CONFIG_DIR/config.toml (then: systemctl --user restart $SERVICE)
      Remove:   $PROJECT_DIR/install.sh --uninstall
EOF
