"""`ftf learn-weights DATASET [--background CORPUS] -o weights.json`: learned stability and background rarity.

The output is JSON (never pickle): the learned `WeightTable`, the background operator counts
(hashed values only) and the learning report. `ftf score --weights weights.json` scores with it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from ftf.contract.models import Dataset
from ftf.linking.background import BackgroundCounts, read_corpus_kinds
from ftf.linking.learned import dump_weights, learn_weights, load_weights
from ftf.linking.ledger import ScoringContext
from ftf.linking.weights import HAND_TABLE


def register_learn_weights(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "learn-weights",
        help="learn per-layer stability from the platform's campaign ids, rarity from a background corpus",
        description="Learn indicator weights: persistence s per layer and kind from pairs inside one aio campaign / "
        "owner (label layer held out), rarity u counted over operators of DATASET and an optional background "
        "corpus. Writes the weight table, hashed background counts and a report (JSON).",
    )
    parser.add_argument("dataset", type=Path, metavar="DATASET.json", help="contract dataset with indicators")
    parser.add_argument("--background", type=Path, metavar="CORPUS.json", help="contract dataset of a background corpus")
    parser.add_argument("--must-not-link", type=Path, metavar="REAL.json",
                        help="contract dataset of real outlet pages: a brand stop-list only, never rarity evidence")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="weights.json")
    parser.set_defaults(func=run_learn_weights)


def run_learn_weights(args: argparse.Namespace) -> int:
    if args.must_not_link and not args.background:
        print("ftf learn-weights: --must-not-link needs --background (the stop-list lives with the corpus counts)", file=sys.stderr)
        return 2
    try:
        ds = Dataset.from_json(args.dataset.read_bytes())
        bg = None
        if args.background:
            mnl = Dataset.from_json(args.must_not_link.read_bytes()) if args.must_not_link else None
            bg = BackgroundCounts.from_dataset(
                Dataset.from_json(args.background.read_bytes()), must_not_link=mnl,
                corpus_kinds=read_corpus_kinds(args.background),
                must_not_link_kinds=read_corpus_kinds(args.must_not_link) if args.must_not_link else None,
            )
        table, report = learn_weights(ds, background=bg)
        dump_weights(args.out, table, report, bg)
    except (OSError, ValidationError, ValueError, KeyError) as exc:
        print(f"ftf learn-weights: {exc}", file=sys.stderr)
        return 2
    if bg is not None:
        print(f"background: {bg.n_operators} operators from {bg.n_incidents} incidents ({bg.sources}); "
              f"brand stop-list {len(bg.brand)} values")
    print(f"learned weights -> {args.out}")
    for rel, info in report["relations"].items():
        if "layers" not in info:
            print(f"  {rel}: {info.get('status')}")
            continue
        print(f"  {rel}: label {info['label_kind']}, {info['n_groups']} groups, {info['n_pos_pairs']} positive / "
              f"{info['n_neg_pairs']} negative pairs, held out {', '.join(info['held_out_layers'])}")
        for layer, li in info["layers"].items():
            if li.get("held_out") or not li.get("n_pos"):
                continue
            print(f"    {layer:22s} s={li['s']:.2f} (prior {li['s_prior']:.2f}, weight {li['shrinkage_weight']:.2f}) "
                  f"pos {li['n_pos_shared']}/{li['n_pos']} neg {li['n_neg_shared']}/{li['n_neg']}")
    return 0


def add_weights_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--weights", type=Path, metavar="weights.json",
                        help="learned weights from `ftf learn-weights` (default: the hand table)")


def load_weights_arg(args: argparse.Namespace):
    """(table, background) from `--weights`, read once; None without the flag. Raises ValueError on a bad file."""
    path = getattr(args, "weights", None)
    if path is None:
        return None
    try:
        table, bg, _ = load_weights(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"cannot read weights {path}: {exc}") from None
    return table, bg


def context_from_args(ds: Dataset, args: argparse.Namespace, loaded=None) -> ScoringContext:
    loaded = loaded if loaded is not None else load_weights_arg(args)
    if loaded is None:
        return ScoringContext.from_dataset(ds, weights=HAND_TABLE)
    table, bg = loaded
    return ScoringContext.from_dataset(ds, weights=table, background=bg)
