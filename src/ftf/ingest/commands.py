"""`ftf ingest`: turn organiser folders, saved pages, HARs, MHTMLs, URLs or fetcher folders into a contract Dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ftf.indicators import redact
from ftf.ingest import adapters, run


ROOT_HELP = """\
--root: artefact paths in the Dataset are relative to it, and every later reader (extractors,
Linking, the app) must open them as <root>/<path>. The default (the common parent of the inputs)
is right for all of these; pass the same folder explicitly to be sure:
  organiser data  ftf ingest /data/raw/do_* /data/raw/BACKUP --root /data/raw --out ds.json
                  (paths: do_udostepnienia/<case>/..., BACKUP/<case>/...)
  fetcher run(s)  ftf ingest <batch>/fetch-1 <batch>/fetch-2 <batch>/fetch-victim-r1 --root <batch> --out ds.json
                  (paths: fetch-1/<capture_id>/page.har; re-fetches of one URL merge across runs)
  scanner run     ftf ingest <batch>/scanner --root <batch> --out ds.json
                  (paths: scanner/<stamp>-<source>-<id>/ad.json)
A --root that is not an ancestor of every input fails. At the end the command prints indicator
counts per layer and warns about artefacts it could not open under the root."""

UNOPENED_SHOWN = 5


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "ingest",
        help="load inputs into contract captures (and extract indicators)",
        description=(
            "Load organiser dataset roots, case folders, saved HTML pages, HAR or MHTML files, bare URLs, "
            "fetcher and scanner capture folders or run folders into one contract Dataset. Captured files "
            "are parsed as inert data; nothing is fetched unless --fetch is given (needs the ftf.fetch extra)."
        ),
        epilog=ROOT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT", help="path or http(s) URL")
    parser.add_argument("--root", type=Path, help="artefact paths are written relative to this folder; see below (default: common parent of the inputs)")
    parser.add_argument("--out", type=Path, required=True, help="write the Dataset JSON here")
    parser.add_argument("--name", default="ingest", help="dataset name (default: %(default)s)")
    parser.add_argument("--no-indicators", dest="indicators", action="store_false", help="captures and artefacts only")
    parser.add_argument("--timing", type=Path, help="write per-capture timing and notes (JSON) here")
    parser.add_argument("--allow-empty", action="store_true",
                        help="succeed even when captures with HTML or HAR yield no indicators at all (normally exit 1)")
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
    s = result.summary()
    if result.unopened:
        shown = ", ".join(redact(p) for p in result.unopened[:UNOPENED_SHOWN])
        more = f" (+{len(result.unopened) - UNOPENED_SHOWN} more)" if len(result.unopened) > UNOPENED_SHOWN else ""
        print(f"ftf ingest: WARNING: {len(result.unopened)} artefacts could not be opened under root {result.root}: {shown}{more}",
              file=sys.stderr)
    if args.indicators and not args.allow_empty and result.empty_pages():
        print(f"ftf ingest: ERROR: {result.empty_pages()} captures with HTML or HAR yielded no indicators at all "
              f"(root {result.root}); check --root and that the extractors are installed, or pass --allow-empty. "
              f"Nothing written to {args.out}", file=sys.stderr)
        return 1
    _write(args.out, result.dataset.to_json())
    if args.timing:
        _write(args.timing, json.dumps(result.timing(), indent=2, ensure_ascii=False))
    layers = ", ".join(f"{k} {v}" for k, v in s["indicators_by_layer"].items()) or "none"
    print(f"ftf ingest: indicators per layer: {layers}", file=sys.stderr)
    print(
        f"ftf ingest: {s['incidents']} incidents, {s['captures']} captures ({s['duplicates']} duplicates), "
        f"{s['artefacts']} artefacts, {s['indicators']} indicators; "
        f"{s['seconds_per_capture_mean']:.3f} s/capture mean, {s['seconds_per_capture_max']:.3f} s max -> {args.out}",
        file=sys.stderr,
    )
    return 0
