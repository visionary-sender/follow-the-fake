"""Command-line entry point: `ftf <command> ...`.

Each feature module adds its own subcommand with `subparsers.add_parser(...)` and sets
`func` via `set_defaults(func=...)`; `main` dispatches to it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ftf import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ftf",
        description="Follow the Fake: group scam ads and landing pages into campaigns.",
    )
    parser.add_argument("--version", action="version", version=f"ftf {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
