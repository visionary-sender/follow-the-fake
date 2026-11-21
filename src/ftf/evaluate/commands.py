"""`ftf eval <sub>`: evaluation harness commands.

Sibling issues add a subcommand with one line: append their `register(subparsers)` function to
`SUBCOMMANDS`. Every subcommand writes JSON with raw counts under `--out-dir`
(default /data/derived/evaluation, override with $FTF_EVAL_DIR).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ftf.contract import Dataset

DEFAULT_OUT = Path(os.environ.get("FTF_EVAL_DIR") or "/data/derived/evaluation")
DEFAULT_PROTOTYPE = Path(os.environ.get("FTF_PROTOTYPE_DIR") or "/data/derived/evaluation/prototype")


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload.to_json() if isinstance(payload, Dataset) else json.dumps(payload, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)
    return path


def add_common(parser: argparse.ArgumentParser) -> None:
    """Options every `ftf eval` subcommand shares."""
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="output root (default: %(default)s)")
    parser.add_argument(
        "--dataset", type=Path,
        help="contract Dataset JSON to evaluate (e.g. Ingestion's output); default: convert the prototype signals",
    )
    parser.add_argument("--prototype-dir", type=Path, default=DEFAULT_PROTOTYPE,
                        help="prototype signals.json + case cards (default: %(default)s)")


def load_dataset(args: argparse.Namespace) -> Dataset:
    """The dataset under evaluation: --dataset if given, else the prototype conversion."""
    if getattr(args, "dataset", None):
        return Dataset.from_json(Path(args.dataset).read_bytes())
    from ftf.evaluate.prototype import build_dataset

    return build_dataset(args.prototype_dir)


def _register_prototype(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("prototype", help="convert the prototype signals into a contract Dataset (stopgap)")
    add_common(p)

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.prototype import build_dataset

        ds = build_dataset(args.prototype_dir)
        write_json(args.out_dir / "prototype-contract" / "dataset.json", ds)
        caps = sum(len(i.captures) for i in ds.incidents)
        print(json.dumps({"incidents": len(ds.incidents), "captures": caps, "indicators": len(ds.indicators())}))
        return 0

    p.set_defaults(func=run)


def _register_labels(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("labels", help="ground-truth labels (platform ids + analyst verdicts) -> labels/labels.json")
    add_common(p)

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.labels import build_labels, compare_expected

        ds = load_dataset(args)
        labels = build_labels(ds)
        report = compare_expected(labels)
        labels["summary"]["e1_discrepancies"] = report
        write_json(args.out_dir / "labels" / "labels.json", labels)
        for line in report:
            print("DISCREPANCY " + line)
        print(json.dumps(labels["summary"], indent=2))
        return 0

    p.set_defaults(func=run)


def _register_leak(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("leak", help="E1 leak check: hide platform ids, search them in what remains")
    add_common(p)

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.leak import MASK, hide_platform_ids, leak_check

        ds = load_dataset(args)
        hidden, values = hide_platform_ids(ds)
        hits = leak_check(hidden, values)
        out = {
            "hidden_indicators": len(ds.indicators()) - len(hidden.indicators()),
            "remaining_indicators": len(hidden.indicators()),
            "hidden_values": len(values),
            "masked_indicators": sum(1 for i in hidden.indicators() if i.value and MASK in i.value),
            "hits": len(hits),
            "hit_rows": [h.as_dict() for h in hits],
        }
        write_json(args.out_dir / "leak" / "leak.json", out)
        print(json.dumps({k: v for k, v in out.items() if k != "hit_rows"}))
        for h in hits[:50]:
            print(f"HIT {h.hidden_kind} in {h.indicator_id} ({h.kind})")
        return 0

    p.set_defaults(func=run)


def _register_baseline(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("baseline", help="cluster with an engine (default: naive baseline) and score per label level")
    add_common(p)
    p.add_argument("--engine", default="baseline", help="engine name: baseline | linking (default: %(default)s)")
    p.add_argument("--hide", action=argparse.BooleanOptionalAction, default=True,
                   help="hide platform ids before clustering (E1; default: on)")

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.engine import get_engine
        from ftf.evaluate.labels import build_labels
        from ftf.evaluate.leak import hide_platform_ids
        from ftf.evaluate.metrics import score_against_labels

        ds = load_dataset(args)
        labels = build_labels(ds)
        engine = get_engine(args.engine)
        work = hide_platform_ids(ds)[0] if args.hide else ds
        clustered = engine.link_dataset(work)
        result = score_against_labels(clustered, labels)
        result.update({"engine": args.engine, "hidden_platform_ids": args.hide})
        write_json(args.out_dir / "baseline" / f"{args.engine}.json", result)
        for row in result["levels"]:
            pw, b3 = row["pairwise"], row["bcubed"]
            print(
                f"{row['relation']:>9} vs {row['level']:<9} pairwise P {pw['tp']}/{pw['predicted_pairs']} "
                f"R {pw['tp']}/{pw['true_pairs']}  B3 P {b3['precision']:.2f} R {b3['recall']:.2f} "
                f"(macro P {row['macro']['precision']:.2f} R {row['macro']['recall']:.2f}) "
                f"must-not-link violated {row['must_not_link']['violated']}/{row['must_not_link']['pairs']}"
            )
        return 0

    p.set_defaults(func=run)


# Sibling issues (HACK-27/28/29) append their register function here.
SUBCOMMANDS: list[Callable[[argparse._SubParsersAction], None]] = [
    _register_prototype,
    _register_labels,
    _register_leak,
    _register_baseline,
]


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("eval", help="evaluation harness (labels, leak check, metrics, baselines)")
    sub = parser.add_subparsers(dest="eval_command", metavar="<sub>")
    for reg in SUBCOMMANDS:
        reg(sub)
    parser.set_defaults(func=lambda args: (parser.print_help(), 0)[1])
