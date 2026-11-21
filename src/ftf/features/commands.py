"""`ftf features`: add visual and content indicators to a contract dataset."""

from __future__ import annotations

import argparse

from ftf.features.common import add_io_args, load_input, write_dataset
from ftf.features.pipeline import FAMILIES, apply_all


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "features", help="extract visual and content features (text, image, faces, video)"
    )
    add_io_args(p)
    p.add_argument(
        "--families",
        default=None,
        help=f"comma-separated subset of {','.join(FAMILIES)} (default: all installed)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ds = load_input(args)
    families = args.families.split(",") if args.families else None
    ds = apply_all(ds, args.raw_root, args.out, families)
    print(write_dataset(ds, args.out))
    return 0
