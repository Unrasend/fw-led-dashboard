from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

from .app import panel_targets, run_dashboard
from .config import ConfigError, DashboardConfig, fill_panel_identities, load_config
from .device_discovery import MatrixDevice, list_system_matrix_devices

PanelLister = Callable[[], Sequence[MatrixDevice]]
DashboardRunner = Callable[[DashboardConfig], int]
GuiRunner = Callable[[str], int]


def _run_gui(mode: str) -> int:
    try:
        from .tray import run_gui
    except (ImportError, ValueError) as error:
        print(
            f"the tray and the settings window need GTK 3 and PyGObject ({error}); "
            "running install.sh again offers to install them",
            file=sys.stderr,
        )
        return 2
    return run_gui(mode)


def main(
    argv: Sequence[str] | None = None,
    *,
    panel_lister: PanelLister = list_system_matrix_devices,
    dashboard_runner: DashboardRunner = run_dashboard,
    gui_runner: GuiRunner = _run_gui,
) -> int:
    parser = argparse.ArgumentParser(prog="framework-led-dashboard")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check-config",
        metavar="PATH",
        help="validate PATH and print its normalized configuration",
    )
    action.add_argument(
        "--list-panels",
        action="store_true",
        help="list LED Matrix USB identities for calibration",
    )
    action.add_argument(
        "--run",
        metavar="PATH",
        help="run the dashboard on both panels with configuration PATH until SIGTERM",
    )
    action.add_argument(
        "--tray",
        action="store_true",
        help="show the tray icon (the one the desktop supports)",
    )
    action.add_argument(
        "--settings",
        action="store_true",
        help="open the settings window (in the running tray, if there is one)",
    )
    action.add_argument(
        "--init-config",
        nargs=2,
        metavar=("TEMPLATE", "PATH"),
        help="write PATH from TEMPLATE with the two connected panels filled in",
    )
    arguments = parser.parse_args(argv)

    if arguments.init_config is not None:
        return _init_config(*map(Path, arguments.init_config), panel_lister=panel_lister)

    if arguments.tray or arguments.settings:
        return gui_runner("tray" if arguments.tray else "settings")

    if arguments.run is not None:
        try:
            config = load_config(arguments.run)
            panel_targets(config)
        except (ConfigError, OSError) as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 2
        logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(threadName)s] %(message)s")
        return dashboard_runner(config)

    if arguments.list_panels:
        try:
            devices = panel_lister()
        except OSError as error:
            print(f"device discovery error: {error}", file=sys.stderr)
            return 2

        payload = {
            "panels": [
                {
                    "identity": device.identity,
                    "location": device.location,
                    "path": device.path,
                    "product": device.product,
                    "serial_number": device.serial_number,
                }
                for device in devices
            ]
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    try:
        config = load_config(arguments.check_config)
    except (ConfigError, OSError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(config), indent=2, sort_keys=True))
    return 0


def _init_config(template: Path, target: Path, *, panel_lister: PanelLister) -> int:
    if target.exists():
        print(f"{target} already exists; edit it instead", file=sys.stderr)
        return 2
    try:
        identities = sorted(
            (device.identity for device in panel_lister() if device.identity), reverse=True
        )
    except OSError as error:
        print(f"device discovery error: {error}", file=sys.stderr)
        return 2
    if len(identities) != 2:
        print(
            f"found {len(identities)} LED Matrix panels ({', '.join(identities) or 'none'}), "
            "need exactly 2; connect both, or copy the template and fill panels.*.identity "
            "from --list-panels",
            file=sys.stderr,
        )
        return 2
    # On the Framework 16 this was calibrated on, the module on the higher hub port is the
    # left one; the tray's "Swap panels" switch fixes it if another unit differs.
    left, right = identities
    try:
        filled = fill_panel_identities(template.read_text(encoding="utf-8"), left=left, right=right)
    except (ConfigError, OSError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(filled, encoding="utf-8")
    print(f"wrote {target}: left panel {left}, right panel {right}")
    print('If the bars appear on the right panel, turn on "Swap panels" in the tray menu.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
