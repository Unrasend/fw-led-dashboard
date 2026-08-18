from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict

from .config import ConfigError, load_config
from .device_discovery import MatrixDevice, list_system_matrix_devices


PanelLister = Callable[[], Sequence[MatrixDevice]]


def main(
    argv: Sequence[str] | None = None,
    *,
    panel_lister: PanelLister = list_system_matrix_devices,
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
    arguments = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
