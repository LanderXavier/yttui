"""Entry point: `yttui` y `yttui --setup`."""
from __future__ import annotations

import argparse
import curses
import sys

from .app import main as tui_main
from .setup_wizard import run_setup


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yttui", description="Reproductor YouTube Music en terminal (sin ads).")
    p.add_argument("--setup", action="store_true", help="Configura cookies y login en 1 paso")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.setup:
        return run_setup()
    try:
        curses.wrapper(tui_main)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
