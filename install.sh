#!/usr/bin/env bash
# Install, update or remove the Framework LED Matrix dashboard for the current user.
#
#   ./install.sh              install or update from this checkout (safe to re-run)
#   ./install.sh --uninstall  stop and remove the service, tray entry and venv; keep the config
#
# Without a checkout - piped from the web - it first downloads and unpacks release $VERSION:
#   wget -qO- https://github.com/Unrasend/fw-led-dashboard/releases/latest/download/install.sh | bash
#   wget -qO- https://github.com/Unrasend/fw-led-dashboard/releases/latest/download/install.sh | bash -s -- --uninstall
#
# Installs:
#   ~/.local/share/framework-led-dashboard/venv   venv reusing system pyserial/PyGObject/pycairo
#   ~/.local/share/framework-led-dashboard/src    the unpacked release (piped installs only)
#   ~/.config/framework-led-dashboard/config.toml created once, with both panels detected
#   ~/.config/systemd/user/framework-led-dashboard.service   enabled and (re)started
#   ~/.config/autostart/framework-led-dashboard-tray.desktop  tray icon at login
# An older inputmodule-rs visualizer service (framework-matrix.service), if present, is
# disabled - never deleted - because only one program can own the panels.
set -euo pipefail

VERSION=1.1.0
REPOSITORY_URL=https://github.com/Unrasend/fw-led-dashboard
ARCHIVE_URL=${LED_DASHBOARD_ARCHIVE_URL:-$REPOSITORY_URL/releases/download/v$VERSION/framework-led-dashboard-$VERSION.tar.gz}
PIPE_COMMAND="wget -qO- $REPOSITORY_URL/releases/latest/download/install.sh | bash"

DATA_DIR="$HOME/.local/share/framework-led-dashboard"
CONFIG_DIR="$HOME/.config/framework-led-dashboard"
STATE_DIR="$HOME/.local/state/framework-led-dashboard"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_FILE="$HOME/.config/autostart/framework-led-dashboard-tray.desktop"
VENV="$DATA_DIR/venv"
SRC_DIR="$DATA_DIR/src"
BIN="$VENV/bin/framework-led-dashboard"
SERVICE=framework-led-dashboard.service
OLD_SERVICE=framework-matrix.service
PYTHON=${PYTHON:-python3}

# Empty when the script is piped into bash: there is no checkout to install from.
SCRIPT_PATH=${BASH_SOURCE[0]:-}
PROJECT_DIR=""
if [[ -n "$SCRIPT_PATH" && -f "$(dirname "$SCRIPT_PATH")/packaging/$SERVICE" ]]; then
    PROJECT_DIR=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
fi

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Install, update or remove the Framework LED Matrix dashboard for the current user.

  ./install.sh              install or update from this checkout (safe to re-run)
  ./install.sh --uninstall  stop and remove the service, tray entry and venv; keep the config

Without a checkout:
  $PIPE_COMMAND
  $PIPE_COMMAND -s -- --uninstall
EOF
}

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
    rm -rf "$VENV" "$SRC_DIR"
    rmdir "$DATA_DIR" 2>/dev/null || true
    printf '\nKept your settings: %s and %s (delete them to remove everything).\n' \
        "$CONFIG_DIR" "$STATE_DIR"
    if systemctl --user cat "$OLD_SERVICE" >/dev/null 2>&1; then
        printf 'The old visualizer is still installed: systemctl --user enable --now %s\n' \
            "$OLD_SERVICE"
    fi
}

download() {
    if command -v wget >/dev/null; then
        wget -qO "$2" "$1"
    elif command -v curl >/dev/null; then
        curl -fsSL -o "$2" "$1"
    else
        fail "wget or curl is needed to download the release"
    fi
}

fetch_release() {
    local archive
    step "Downloading release $VERSION"
    command -v tar >/dev/null || fail "tar is required"
    mkdir -p "$DATA_DIR"
    archive=$(mktemp "$DATA_DIR/release-XXXXXX")
    if ! download "$ARCHIVE_URL" "$archive"; then
        rm -f "$archive"
        fail "could not download $ARCHIVE_URL"
    fi
    rm -rf "$SRC_DIR.new"
    mkdir -p "$SRC_DIR.new"
    if ! tar -xzf "$archive" -C "$SRC_DIR.new" --strip-components=1; then
        rm -rf "$archive" "$SRC_DIR.new"
        fail "$ARCHIVE_URL is not a valid release archive"
    fi
    rm -f "$archive"
    [[ -f "$SRC_DIR.new/install.sh" && -f "$SRC_DIR.new/packaging/$SERVICE" ]] \
        || { rm -rf "$SRC_DIR.new"; fail "the release archive is incomplete"; }
    rm -rf "$SRC_DIR"
    mv "$SRC_DIR.new" "$SRC_DIR"
    printf 'unpacked into %s\n' "$SRC_DIR"
}

install_dashboard() {
    step "Checking requirements"
    command -v systemctl >/dev/null || fail "systemd is required"
    command -v "$PYTHON" >/dev/null || { packages_hint; fail "$PYTHON not found"; }
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
        || fail "$PYTHON is older than 3.11"
    "$PYTHON" -c 'import venv, serial' 2>/dev/null \
        || { packages_hint; fail "python venv or pyserial is missing"; }
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
    local update=$PIPE_COMMAND
    if [[ -d "$PROJECT_DIR/.git" ]]; then
        update="cd $PROJECT_DIR && git pull && ./install.sh"
    fi
    cat <<EOF

Done. Logs:     journalctl --user -u $SERVICE -f
      Config:   $CONFIG_DIR/config.toml (then: systemctl --user restart $SERVICE)
      Update:   $update
      Remove:   $PROJECT_DIR/install.sh --uninstall
EOF
}

main() {
    case "${1:-}" in
        "") ;;
        --uninstall) uninstall; return ;;
        -h | --help) usage; return ;;
        *) fail "unknown option: $1 (use --uninstall or --help)" ;;
    esac
    if [[ -z "$PROJECT_DIR" ]]; then
        fetch_release
        # Run the unpacked copy; stdin is still the rest of the piped script.
        exec bash "$SRC_DIR/install.sh" </dev/null
    fi
    install_dashboard
}

# The only top-level command: a download cut short runs nothing.
main "$@"
