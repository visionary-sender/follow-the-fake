"""Command-line entry point: `ftf <command> ...`.

A feature module adds a subcommand by defining `register(subparsers)`, which calls
`subparsers.add_parser(...)` and `set_defaults(func=...)`, and by being listed in `COMMANDS`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence

from ftf import __version__
from ftf.enrich.commands import register as register_enrich_domain
from ftf.features.commands import register as register_features
from ftf.linking.commands import register_score
from ftf.pipeline.commands import register_analyze
from ftf.pipeline.commands import register_serve

COMMANDS: list[Callable[[argparse._SubParsersAction], None]] = [
    register_analyze,
    register_enrich_domain,
    register_features,
    register_score,
    register_serve,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ftf",
        description="Follow the Fake: group scam ads and landing pages into campaigns.",
    )
    parser.add_argument("--version", action="version", version=f"ftf {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for register in COMMANDS:
        register(subparsers)
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
