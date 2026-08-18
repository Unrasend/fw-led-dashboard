from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from .config import ConfigError, load_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framework-led-dashboard")
    parser.add_argument(
        "--check-config",
        metavar="PATH",
        help="validate PATH and print its normalized configuration",
    )
    arguments = parser.parse_args(argv)

    if arguments.check_config is None:
        parser.error("--check-config PATH is required until the dashboard runtime is implemented")

    try:
        config = load_config(arguments.check_config)
    except (ConfigError, OSError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
