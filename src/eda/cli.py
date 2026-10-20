"""Entry point for the `eda` command. Subcommands are added from M0 onward."""

import argparse
import sys
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eda", description="AI-native EDA toolchain")
    parser.add_argument("--version", action="version", version=f"eda {version('eda')}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
