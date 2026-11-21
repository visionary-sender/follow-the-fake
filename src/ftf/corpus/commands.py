"""`ftf corpus targets|build`: background corpus from scanner output and fetcher captures.

    ftf corpus targets --queue Q [--run R ...] [--picked LIST] --out DIR [--parts 2]
        write DIR/urls-<n>.txt (input for `ftf fetch --list`) and DIR/targets.jsonl
    ftf corpus build --root DIR [--scanner SUB] [--fetch SUB ...] [--picked LIST --picked-fetch SUB]
        read what sits under DIR, write DIR/dataset.json, DIR/manifest.jsonl, DIR/summary.json

Nothing here touches the network: fetching is `ftf fetch` (HACK-17) on the sandbox.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ftf.corpus.targets import Target, build_targets, read_picked, read_queue, split


def _targets(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    targets: list[Target] = []
    if args.queue:
        targets += build_targets(read_queue(Path(args.queue), args.run or None))
    if args.picked:
        seen = {t.url for t in targets}
        for p in read_picked(Path(args.picked)):
            if p.url not in seen:
                targets.append(Target(p.url, [f"picked:{p.brand}"], ["picked"]))
    if args.limit:
        targets = targets[: args.limit]
    for n, part in enumerate(split(targets, args.parts), 1):
        (out / f"urls-{n}.txt").write_text("".join(t.url + "\n" for t in part), encoding="utf-8")
    with open(out / "targets.jsonl", "w", encoding="utf-8") as fh:
        for t in targets:
            fh.write(json.dumps({"url": t.url, "keys": t.keys, "sources": t.sources}) + "\n")
    print(f"{len(targets)} targets in {args.parts} list(s) under {out}")
    return 0


def _build(args: argparse.Namespace) -> int:
    from ftf.corpus.build import build_corpus

    summary = build_corpus(
        root=Path(args.root),
        scanner=args.scanner,
        runs=args.run or None,
        fetch_dirs=args.fetch or [],
        picked=Path(args.picked) if args.picked else None,
        picked_fetch_dirs=args.picked_fetch or [],
        name=args.name,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("corpus", help="background corpus: fetch targets and packaging")
    sub = p.add_subparsers(dest="corpus_command", metavar="<step>", required=True)

    t = sub.add_parser("targets", help="write URL lists for `ftf fetch --list`")
    t.add_argument("--queue", help="scanner queue.jsonl")
    t.add_argument("--run", action="append", help="keep only these scanner run ids (repeatable)")
    t.add_argument("--picked", help="hand-picked TSV list (url, brand, page_kind[, note])")
    t.add_argument("--out", required=True, help="output directory")
    t.add_argument("--parts", type=int, default=1, help="number of URL lists (default 1)")
    t.add_argument("--limit", type=int, default=0, help="keep only the first N targets")
    t.set_defaults(func=_targets)

    b = sub.add_parser("build", help="package scanner items and captures into a contract dataset")
    b.add_argument("--root", required=True, help="corpus directory; all paths are relative to it")
    b.add_argument("--name", default="corpus", help="dataset name")
    b.add_argument("--scanner", help="subdir with the scanner snapshot (queue.jsonl, runs/, creatives/)")
    b.add_argument("--run", action="append", help="keep only these scanner run ids (repeatable)")
    b.add_argument("--fetch", action="append", help="subdir of `ftf fetch` output for queue targets")
    b.add_argument("--picked", help="hand-picked must-not-link TSV list")
    b.add_argument("--picked-fetch", action="append", help="subdir of `ftf fetch` output for --picked")
    b.set_defaults(func=_build)
