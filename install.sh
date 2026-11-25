#!/usr/bin/env bash
# Install, update or remove the Framework LED Matrix dashboard for the current user.
#
#   ./install.sh               install or update from this checkout (safe to re-run)
#   ./install.sh --yes         ...and install missing system packages without asking
#   ./install.sh --no-packages ...and never touch system packages
#   ./install.sh --docker      run the dashboard in a Docker container instead (optional)
#   ./install.sh --native      back from Docker to the plain user service
#   ./install.sh --uninstall   remove the service, tray, menu entry and program; keep settings
# Without --docker or --native an update keeps the variant that is installed.
#
# Without a checkout - piped from the web - it first downloads and unpacks release $VERSION:
#   wget -qO- https://github.com/Unrasend/fw-led-dashboard/releases/latest/download/install.sh | bash
#   wget -qO- https://github.com/Unrasend/fw-led-dashboard/releases/latest/download/install.sh | bash -s -- --uninstall
#
# The dashboard itself needs only Python 3.11+. For the rest it asks once, then installs with
# your package manager (pacman, apt, dnf, zypper): PyGObject and GTK 3 for the tray and the
# settings window, parec or pw-record for the equalizer, and on GNOME the AppIndicator
# extension that gives it a tray.
#
# Installs:
#   ~/.local/share/framework-led-dashboard/lib    the program (bin/ starts it with python3)
#   ~/.local/share/framework-led-dashboard/src    the unpacked release (piped installs only)
#   ~/.config/framework-led-dashboard/config.toml created once, with both panels detected
#   ~/.config/systemd/user/framework-led-dashboard.service   enabled and (re)started
#   ~/.config/autostart/framework-led-dashboard-tray.desktop  tray icon at login
#   ~/.local/share/applications, ~/.local/share/icons         "LED Matrix Dashboard" settings
# With --docker the service runs the image framework-led-dashboard:$VERSION, built here from
# the Dockerfile, through bin/framework-led-dashboard-docker; the tray and the settings window
# stay on the desktop.
# An older inputmodule-rs visualizer service (framework-matrix.service), if present, is
# disabled - never deleted - because only one program can own the panels.
set -euo pipefail

VERSION=1.6.0
REPOSITORY_URL=https://github.com/Unrasend/fw-led-dashboard
ARCHIVE_URL=${LED_DASHBOARD_ARCHIVE_URL:-$REPOSITORY_URL/releases/download/v$VERSION/framework-led-dashboard-$VERSION.tar.gz}
PIPE_COMMAND="wget -qO- $REPOSITORY_URL/releases/latest/download/install.sh | bash"

APP_ID=io.github.unrasend.FwLedDashboard
DATA_DIR="$HOME/.local/share/framework-led-dashboard"
CONFIG_DIR="$HOME/.config/framework-led-dashboard"
STATE_DIR="$HOME/.local/state/framework-led-dashboard"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_FILE="$HOME/.config/autostart/framework-led-dashboard-tray.desktop"
MENU_FILE="$HOME/.local/share/applications/$APP_ID.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/framework-led-dashboard.svg"
LIB_DIR="$DATA_DIR/lib"
BIN_DIR="$DATA_DIR/bin"
SRC_DIR="$DATA_DIR/src"
OLD_VENV="$DATA_DIR/venv"  # versions before 1.3 installed into a venv
BIN="$BIN_DIR/framework-led-dashboard"
DOCKER_BIN="$BIN_DIR/framework-led-dashboard-docker"
DOCKER_IMAGE=framework-led-dashboard
SERVICE=framework-led-dashboard.service
OLD_SERVICE=framework-matrix.service
GNOME_EXTENSION=appindicatorsupport@rgcjonas.gmail.com
PYTHON=${PYTHON:-python3}
ASSUME_YES=0
SKIP_PACKAGES=0
MODE=""  # docker or native; empty keeps the installed variant (native on a first install)

# Empty when the script is piped into bash: there is no checkout to install from.
SCRIPT_PATH=${BASH_SOURCE[0]:-}
PROJECT_DIR=""
if [[ -n "$SCRIPT_PATH" && -f "$(dirname "$SCRIPT_PATH")/packaging/$SERVICE" ]]; then
    PROJECT_DIR=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
fi

step() { printf '\n==> %s\n' "$*"; }
note() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Install, update or remove the Framework LED Matrix dashboard for the current user.

  ./install.sh               install or update from this checkout (safe to re-run)
  ./install.sh --yes         ...and install missing system packages without asking
  ./install.sh --no-packages ...and never touch system packages
  ./install.sh --docker      run the dashboard in a Docker container instead
  ./install.sh --native      back from Docker to the plain user service
  ./install.sh --uninstall   remove the service, tray, menu entry and program; keep settings

Without a checkout:
  $PIPE_COMMAND
  $PIPE_COMMAND -s -- --uninstall
EOF
}

# --- system packages ------------------------------------------------------------------------

package_manager() {
    if [[ -e /run/ostree-booted ]]; then
        echo rpm-ostree  # Silverblue, Kinoite, Bazzite: layering needs a reboot, so only advise
    elif command -v pacman >/dev/null; then
        echo pacman
    elif command -v apt-get >/dev/null; then
        echo apt
    elif command -v dnf >/dev/null; then
        echo dnf
    elif command -v zypper >/dev/null; then
        echo zypper
    else
        echo unknown
    fi
}

# packages_for MANAGER NEED - the distribution's package names for gtk, audio or gnome-tray.
packages_for() {
    case "$1:$2" in
        pacman:gtk) echo "python-gobject python-cairo gtk3" ;;
        apt:gtk) echo "python3-gi python3-gi-cairo gir1.2-gtk-3.0" ;;
        dnf:gtk | rpm-ostree:gtk) echo "python3-gobject gtk3" ;;
        zypper:gtk) echo "python3-gobject python3-gobject-cairo python3-gobject-Gdk typelib-1_0-Gtk-3_0" ;;
        pacman:audio) echo "libpulse" ;;
        apt:audio | dnf:audio | rpm-ostree:audio | zypper:audio) echo "pulseaudio-utils" ;;
        *:gnome-tray) echo "gnome-shell-extension-appindicator" ;;
        *) echo "" ;;
    esac
}

on_gnome() {
    [[ ":${XDG_CURRENT_DESKTOP:-}:" == *:GNOME:* ]]
}

has_tray_host() {
    busctl --user --no-pager status org.kde.StatusNotifierWatcher >/dev/null 2>&1
}

# missing_needs - which of gtk, audio, gnome-tray this system lacks, one per line. In Docker
# mode the container brings its own audio tools, so that need is not asked for.
missing_needs() {
    # The settings window draws the panels with cairo, which PyGObject reaches through pycairo.
    if ! "$PYTHON" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk
gi.require_foreign("cairo")' >/dev/null 2>&1; then
        echo gtk
    fi
    if [[ "$MODE" != docker ]] && ! command -v parec >/dev/null \
        && ! command -v pw-record >/dev/null; then
        echo audio
    fi
    if on_gnome && ! has_tray_host \
        && ! gnome-extensions info "$GNOME_EXTENSION" >/dev/null 2>&1; then
        echo gnome-tray
    fi
}

ask_yes() {
    [[ "$ASSUME_YES" == 1 ]] && return 0
    # Piped installs read the script from stdin, so ask on the terminal itself.
    { : </dev/tty; } 2>/dev/null || return 1
    local answer=""
    printf '%s [Y/n] ' "$1" >/dev/tty
    read -r answer </dev/tty || return 1
    [[ -z "$answer" || "$answer" == [Yy]* ]]
}

install_command() {
    local manager=$1
    shift
    case "$manager" in
        pacman) echo "sudo pacman -S --needed $*" ;;
        apt) echo "sudo apt-get install $*" ;;
        dnf) echo "sudo dnf install $*" ;;
        zypper) echo "sudo zypper install $*" ;;
        rpm-ostree) echo "rpm-ostree install $* (then reboot)" ;;
        *) echo "install with your package manager: $*" ;;
    esac
}

run_install() {
    local manager=$1 input=/dev/null
    shift
    # sudo asks for the password on the terminal; piped installs have no other stdin.
    { : </dev/tty; } 2>/dev/null && input=/dev/tty
    case "$manager" in
        pacman) sudo pacman -S --needed --noconfirm "$@" ;;
        apt) sudo apt-get install -y "$@" ;;
        dnf) sudo dnf install -y "$@" ;;
        zypper) sudo zypper --non-interactive install "$@" ;;
        *) return 1 ;;
    esac <"$input"
}

enable_gnome_tray() {
    command -v gnome-extensions >/dev/null || return 0
    if gnome-extensions enable "$GNOME_EXTENSION" 2>/dev/null; then
        note "enabled the AppIndicator extension"
        return 0
    fi
    # A freshly installed extension is only seen after logging in again: enable it for then.
    local enabled
    enabled=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")
    if [[ "$enabled" != *"$GNOME_EXTENSION"* ]]; then
        enabled=${enabled/@as /}
        enabled=${enabled%]}
        [[ "$enabled" == "[" ]] && enabled="['$GNOME_EXTENSION']" || enabled="$enabled, '$GNOME_EXTENSION']"
        gsettings set org.gnome.shell enabled-extensions "$enabled" 2>/dev/null || true
    fi
    note "log out and back in once to see the tray icon (GNOME loads new extensions at login)"
}

install_system_packages() {
    local needs manager packages=() need
    mapfile -t needs < <(missing_needs)
    [[ ${#needs[@]} -eq 0 ]] && return 0
    manager=$(package_manager)
    for need in "${needs[@]}"; do
        read -ra names <<<"$(packages_for "$manager" "$need")"
        packages+=("${names[@]}")
    done
    step "System packages"
    for need in "${needs[@]}"; do
        case "$need" in
            gtk) note "- PyGObject with cairo and GTK 3: the tray icon and the settings window" ;;
            audio) note "- parec (PulseAudio tools): the equalizer" ;;
            gnome-tray) note "- GNOME's AppIndicator extension: GNOME has no tray without it" ;;
        esac
    done
    local command
    command=$(install_command "$manager" "${packages[@]}")
    if [[ "$SKIP_PACKAGES" == 1 || "$manager" == rpm-ostree || "$manager" == unknown ]]; then
        warn "not installed; to add them: $command"
        return 0
    fi
    if ! ask_yes "Install them now ($command)?"; then
        warn "skipped; the dashboard runs without them. Later: $command"
        return 0
    fi
    if ! run_install "$manager" "${packages[@]}"; then
        warn "the package manager failed; the dashboard runs without them. Later: $command"
        return 0
    fi
    [[ " ${needs[*]} " == *" gnome-tray "* ]] && enable_gnome_tray
    return 0
}

# --- the program ----------------------------------------------------------------------------

stop_tray() {
    # Whole command lines only, so no other process that mentions the tray is hit: the
    # launcher execs `python3 -P -m framework_led_dashboard --tray`; before 1.3 a venv script.
    pkill -f '^/[^ ]*python[^ ]* -P -m framework_led_dashboard --tray$' 2>/dev/null || true
    pkill -f "^[^ ]*python[^ ]* $OLD_VENV/bin/framework-led-dashboard --tray\$" 2>/dev/null || true
}

# installed_mode - "docker" if the installed service runs the container, else "native".
installed_mode() {
    if grep -qs framework-led-dashboard-docker "$UNIT_DIR/$SERVICE"; then
        echo docker
    else
        echo native
    fi
}

# remove_docker_images [KEEP] - every framework-led-dashboard image but the tag KEEP.
remove_docker_images() {
    command -v docker >/dev/null || return 0
    local image
    docker image ls --format '{{.Repository}}:{{.Tag}}' "$DOCKER_IMAGE" 2>/dev/null \
        | while read -r image; do
            [[ "$image" == "$DOCKER_IMAGE:${1:-}" ]] || docker image rm "$image" >/dev/null 2>&1 || true
        done
}

install_docker_image() {
    step "Building the Docker image $DOCKER_IMAGE:$VERSION"
    command -v docker >/dev/null || fail "docker is not installed; install it, or install without --docker"
    docker info >/dev/null 2>&1 \
        || fail "cannot reach Docker: is it running, and are you in the docker group?"
    # The build only installs Alpine packages. The host's network spares it a VPN that blocks
    # containers' DNS; the dashboard's own container gets a network of its own.
    docker build --network host --tag "$DOCKER_IMAGE:$VERSION" \
        --build-arg "VERSION=$VERSION" "$PROJECT_DIR" || fail "the Docker image did not build"
    remove_docker_images "$VERSION"
    sed "s|@VERSION@|$VERSION|" "$PROJECT_DIR/packaging/framework-led-dashboard-docker" \
        >"$DOCKER_BIN.new"
    chmod 755 "$DOCKER_BIN.new"
    mv "$DOCKER_BIN.new" "$DOCKER_BIN"
}

uninstall() {
    step "Removing $SERVICE"
    systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    systemctl --user daemon-reload
    if command -v docker >/dev/null; then
        docker rm --force framework-led-dashboard >/dev/null 2>&1 || true
        docker network rm framework-led-dashboard >/dev/null 2>&1 || true
        remove_docker_images
    fi
    step "Removing the tray and the menu entry"
    stop_tray
    rm -f "$AUTOSTART_FILE" "$MENU_FILE" "$ICON_FILE"
    step "Removing $DATA_DIR"
    rm -rf "$LIB_DIR" "$BIN_DIR" "$SRC_DIR" "$OLD_VENV"
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

install_program() {
    local python_path
    python_path=$(command -v "$PYTHON")
    step "Installing the program into $DATA_DIR"
    rm -rf "$LIB_DIR.new"
    mkdir -p "$LIB_DIR.new" "$BIN_DIR"
    cp -R "$PROJECT_DIR/src/framework_led_dashboard" "$LIB_DIR.new/"
    find "$LIB_DIR.new" -name __pycache__ -type d -prune -exec rm -rf {} +
    rm -rf "$LIB_DIR"
    mv "$LIB_DIR.new" "$LIB_DIR"
    cat >"$BIN.new" <<EOF
#!/bin/sh
# Starts the Framework LED Matrix dashboard that install.sh put in $LIB_DIR.
PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec "$python_path" -P -m framework_led_dashboard "\$@"
EOF
    chmod 755 "$BIN.new"
    mv "$BIN.new" "$BIN"
    "$BIN" --help >/dev/null || fail "the installed program does not start"
    rm -rf "$OLD_VENV"
}

install_desktop_entries() {
    step "Tray icon at login and the LED Matrix Dashboard menu entry"
    install -Dm644 "$PROJECT_DIR/packaging/framework-led-dashboard.svg" "$ICON_FILE"
    mkdir -p "$(dirname "$AUTOSTART_FILE")" "$(dirname "$MENU_FILE")"
    sed "s|@BIN@|$BIN|" "$PROJECT_DIR/packaging/framework-led-dashboard-tray.desktop" \
        >"$AUTOSTART_FILE"
    sed "s|@BIN@|$BIN|" "$PROJECT_DIR/packaging/$APP_ID.desktop" >"$MENU_FILE"
    command -v update-desktop-database >/dev/null \
        && update-desktop-database "$(dirname "$MENU_FILE")" 2>/dev/null || true
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        stop_tray
        setsid -f "$BIN" --tray >/dev/null 2>&1
        note "tray (re)started"
    else
        note "no graphical session in this shell: the tray starts at the next login"
    fi
}

install_dashboard() {
    local installed
    installed=$(installed_mode)
    MODE=${MODE:-$installed}
    step "Checking requirements"
    command -v systemctl >/dev/null || fail "systemd is required"
    command -v "$PYTHON" >/dev/null || fail "$PYTHON not found; install Python 3.11 or newer"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
        || fail "$PYTHON is older than 3.11; this distribution is too old for the dashboard"
    install_system_packages

    install_program
    if [[ "$MODE" == docker ]]; then
        install_docker_image
    fi

    step "Configuration in $CONFIG_DIR"
    if [[ -f "$CONFIG_DIR/config.toml" ]]; then
        note "keeping the existing config.toml"
    elif [[ -f "$PROJECT_DIR/config.toml" ]]; then
        install -Dm644 "$PROJECT_DIR/config.toml" "$CONFIG_DIR/config.toml"
        note "copied $PROJECT_DIR/config.toml"
    else
        "$BIN" --init-config "$PROJECT_DIR/config.example.toml" "$CONFIG_DIR/config.toml" \
            || fail "could not detect both panels; see '$BIN --list-panels'"
    fi
    "$BIN" --check-config "$CONFIG_DIR/config.toml" >/dev/null \
        || fail "$CONFIG_DIR/config.toml is invalid"

    step "Checking that no hand-started dashboard holds the panels"
    if ! systemctl --user --quiet is-active "$SERVICE" \
        && PYTHONPATH="$LIB_DIR" "$PYTHON" -c 'from framework_led_dashboard.control import ControlClient; ControlClient().status()' 2>/dev/null; then
        fail "a dashboard started by hand is running; stop it (Ctrl+C or kill) and re-run"
    fi

    if systemctl --user cat "$OLD_SERVICE" >/dev/null 2>&1; then
        step "Disabling the old visualizer $OLD_SERVICE (kept for rollback)"
        systemctl --user disable --now framework-matrix.service
    fi

    local unit=packaging/framework-led-dashboard.service
    [[ "$MODE" == docker ]] && unit=packaging/framework-led-dashboard-docker.service
    step "Installing and starting $SERVICE ($MODE)"
    if [[ "$MODE" != "$installed" ]]; then
        # Stop it with the unit that started it: the Docker one removes its container.
        systemctl --user stop "$SERVICE" 2>/dev/null || true
    fi
    install -Dm644 "$PROJECT_DIR/$unit" "$UNIT_DIR/$SERVICE"
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE"
    systemctl --user restart "$SERVICE"
    if [[ "$MODE" == native && "$installed" == docker ]]; then
        rm -f "$DOCKER_BIN"
        docker network rm framework-led-dashboard >/dev/null 2>&1 || true
        remove_docker_images
        note "removed the Docker variant"
    fi

    install_desktop_entries

    step "Status"
    sleep 2
    systemctl --user --no-pager --lines=8 status "$SERVICE" || true
    local update=$PIPE_COMMAND
    if [[ -d "$PROJECT_DIR/.git" ]]; then
        update="cd $PROJECT_DIR && git pull && ./install.sh"
    fi
    cat <<EOF

Done ($MODE). Settings: "LED Matrix Dashboard" in the application menu, or the tray icon
      Logs:     journalctl --user -u $SERVICE -f
      Config:   $CONFIG_DIR/config.toml (then: systemctl --user restart $SERVICE)
      Update:   $update
      Remove:   $PROJECT_DIR/install.sh --uninstall
EOF
}

main() {
    local arguments=("$@")
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --uninstall) uninstall; return ;;
            -h | --help) usage; return ;;
            -y | --yes) ASSUME_YES=1 ;;
            --no-packages) SKIP_PACKAGES=1 ;;
            --docker) MODE=docker ;;
            --native) MODE=native ;;
            *) fail "unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    if [[ -z "$PROJECT_DIR" ]]; then
        fetch_release
        # Run the unpacked copy; stdin is still the rest of the piped script.
        exec bash "$SRC_DIR/install.sh" "${arguments[@]}" </dev/null
    fi
    install_dashboard
}

# The only top-level command, so a download cut short runs nothing; tests source the
# functions without running it.
if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
