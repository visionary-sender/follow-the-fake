"""`ftf ingest`: turn organiser folders, saved pages, HARs, MHTMLs, URLs or fetcher folders into a contract Dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ftf.ingest import adapters, run


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "ingest",
        help="load inputs into contract captures (and extract indicators)",
        description=(
            "Load organiser dataset roots, case folders, saved HTML pages, HAR or MHTML files, bare URLs "
            "and fetcher capture folders into one contract Dataset. Captured files are parsed as inert "
            "data; nothing is fetched unless --fetch is given (needs the ftf.fetch extra)."
        ),
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT", help="path or http(s) URL")
    parser.add_argument("--root", type=Path, help="artefact paths are relative to this folder (default: common parent of the inputs)")
    parser.add_argument("--out", type=Path, required=True, help="write the Dataset JSON here")
    parser.add_argument("--name", default="ingest", help="dataset name (default: %(default)s)")
    parser.add_argument("--no-indicators", dest="indicators", action="store_false", help="captures and artefacts only")
    parser.add_argument("--timing", type=Path, help="write per-capture timing and notes (JSON) here")
    parser.add_argument("--fetch", action="store_true", help="capture bare URLs with the HACK-17 fetcher (sandbox only)")
    parser.add_argument("--fetch-dir", type=Path, help="fetcher output folder (default: <out dir>/fetched)")
    parser.set_defaults(func=main)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(args: argparse.Namespace) -> int:
    if args.fetch and not adapters.fetch_available():
        print("ftf ingest: --fetch needs the HACK-17 fetcher (ftf.fetch), which is not installed", file=sys.stderr)
        return 2
    fetch_dir = args.fetch_dir or args.out.parent / "fetched"
    try:
        result = run(args.inputs, root=args.root, name=args.name, indicators=args.indicators, fetch=args.fetch, fetch_dir=fetch_dir)
    except (FileNotFoundError, ValueError) as e:
        print(f"ftf ingest: {e}", file=sys.stderr)
        return 2
    _write(args.out, result.dataset.to_json())
    if args.timing:
        _write(args.timing, json.dumps(result.timing(), indent=2, ensure_ascii=False))
    s = result.summary()
    print(
        f"ftf ingest: {s['incidents']} incidents, {s['captures']} captures ({s['duplicates']} duplicates), "
        f"{s['artefacts']} artefacts, {s['indicators']} indicators; "
        f"{s['seconds_per_capture_mean']:.3f} s/capture mean, {s['seconds_per_capture_max']:.3f} s max -> {args.out}",
        file=sys.stderr,
    )
    return 0
