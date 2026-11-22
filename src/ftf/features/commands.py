"""`ftf features`: add visual and content indicators to a contract dataset.

`ftf features prefetch` downloads the models the families load into ``FTF_MODEL_DIR``, for runs
with ``FTF_OFFLINE=1`` (the jury image bakes them in at build time).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from ftf.features import prefetch as models
from ftf.features.common import load_input, model_dir, write_dataset
from ftf.features.offline import offline
from ftf.features.pipeline import FAMILIES, apply_all, available_families


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "features", help="extract visual and content features (text, image, faces, video)"
    )
    # The I/O flags of add_io_args, with --out optional: `features prefetch` takes no output dir.
    p.add_argument("--dataset", type=Path, help="input contract dataset JSON")
    p.add_argument(
        "--files", type=Path, nargs="+", help="DEV ONLY: raw files to wrap one capture each"
    )
    p.add_argument("--raw-root", type=Path, default=Path("/data/raw"))
    p.add_argument("--out", type=Path, help="output directory (required for extraction)")
    p.add_argument(
        "--families",
        default=None,
        help=f"comma-separated subset of {','.join(FAMILIES)} (default: all installed)",
    )
    p.set_defaults(func=run)
    sub = p.add_subparsers(dest="action", metavar="{prefetch}")
    pf = sub.add_parser(
        "prefetch",
        help="download the models into FTF_MODEL_DIR (text, image, faces; speech with --speech)",
    )
    pf.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="directory to fill (default: FTF_MODEL_DIR or ~/.cache/ftf/models)",
    )
    pf.add_argument(
        "--reference-from",
        default=None,
        help="face reference set from a local directory or gs:// prefix instead of Wikimedia",
    )
    pf.add_argument(
        "--speech", action="store_true", help="also fetch the faster-whisper speech model (video)"
    )


def _families(spec: Optional[str]) -> Optional[list[str]]:
    if spec is None:
        return None
    names = [f.strip() for f in spec.split(",") if f.strip()]
    if not names:
        raise SystemExit("--families is empty")
    return names


def run(args: argparse.Namespace) -> int:
    if args.action == "prefetch":
        models.prefetch(args.model_dir, reference_from=args.reference_from, speech=args.speech)
        return 0
    if args.out is None:
        raise SystemExit("--out is required")
    families = _families(args.families)
    names = families if families is not None else available_families()
    if offline():
        gone = models.missing(names)
        if gone:
            raise SystemExit(
                f"FTF_OFFLINE=1 but these model files are not in {model_dir()}:\n  "
                + "\n  ".join(gone)
                + "\nrun `ftf features prefetch` once with network access (same FTF_MODEL_DIR)"
            )
    ds = load_input(args)
    ds = apply_all(ds, args.raw_root, args.out, names)
    print(write_dataset(ds, args.out))
    return 0
