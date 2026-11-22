"""`ftf eval <sub>`: evaluation harness commands.

Sibling issues add a subcommand with one line: append their `register(subparsers)` function to
`SUBCOMMANDS`, using `add_common` and `load_dataset` for the shared options. Every subcommand
writes JSON with raw counts under `--out-dir` (default /data/derived/evaluation, $FTF_EVAL_DIR).
Every subcommand fails loudly (exit 2) on a dataset without any ground-truth platform ids.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ftf.contract import Dataset
from ftf.evaluate.hidden import register as register_hidden
from ftf.evaluate.report import register as register_report
from ftf.evaluate.timesplit import register as register_timesplit
from ftf.evaluate.units import DEFAULT_SPEC, load_spec

DEFAULT_OUT = Path(os.environ.get("FTF_EVAL_DIR") or "/data/derived/evaluation")
DEFAULT_DATASET = Path(
    os.environ.get("FTF_EVAL_DATASET") or "/data/derived/ingest/organiser/dataset-indicators.json"
)
DEFAULT_CORPUS = Path(
    os.environ.get("FTF_EVAL_CORPUS") or "/data/derived/corpus/batch-1/ingested/dataset.json"
)


class EvalInputError(RuntimeError):
    """The input cannot give honest numbers (e.g. no ground-truth ids); exit non-zero."""


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload.to_json() if isinstance(payload, Dataset) else json.dumps(payload, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)
    return path


def dataset_build(dataset: Path) -> str | None:
    """Build commit of the dataset, read at run time from the BUILD.txt beside it ("build: <branch> <sha>")."""
    build_file = Path(dataset).parent / "BUILD.txt"
    if not build_file.is_file():
        print(f"warning: no {build_file}; dataset_build recorded as null", file=sys.stderr)
        return None
    m = re.search(r"^build:\s*\S+\s+(\S+)", build_file.read_text(encoding="utf-8"), re.M)
    if m is None:
        print(f"warning: no 'build: <branch> <sha>' line in {build_file}; dataset_build recorded as null", file=sys.stderr)
        return None
    return m.group(1)


def add_common(parser: argparse.ArgumentParser) -> None:
    """Options every `ftf eval` subcommand shares."""
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="output root (default: %(default)s)")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                        help="contract Dataset JSON to evaluate (default: Ingestion's organiser dataset, %(default)s)")
    parser.add_argument("--spec", type=Path, default=None,
                        help=f"private ground-truth spec JSON (default: $FTF_EVAL_SPEC or {DEFAULT_SPEC})")


def load_dataset(args: argparse.Namespace) -> Dataset:
    """The dataset under evaluation; refuses one without any ground-truth platform ids."""
    from ftf.evaluate.labels import PLATFORM_LEVELS

    ds = Dataset.from_json(Path(args.dataset).read_bytes())
    kinds = {k for k, _, _ in PLATFORM_LEVELS.values()}
    present = {i.kind for i in ds.indicators() if i.kind in kinds and i.value}
    if not present:
        n = len(ds.indicators())
        raise EvalInputError(
            f"{args.dataset}: {n} indicators, none of the ground-truth kinds ({', '.join(sorted(kinds))}); "
            "refusing to evaluate (captures-only or wrong dataset?)"
        )
    return ds


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _register_labels(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("labels", help="ground-truth labels (platform ids + analyst verdicts) -> labels/labels.json")
    add_common(p)

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.labels import build_labels, compare_expected

        ds = load_dataset(args)
        spec = load_spec(args.spec)
        labels = build_labels(ds, spec)
        labels["dataset_build"] = dataset_build(args.dataset)
        report = compare_expected(labels, spec, ds)
        labels["summary"]["e1_discrepancies"] = report
        write_json(args.out_dir / "labels" / "labels.json", labels)
        for line in report:
            print("DISCREPANCY " + line)
        for m in labels["summary"]["missing_captures"]:
            print(f"MISSING capture {m['capture']} named in {m['in']}")
        print(json.dumps({k: v for k, v in labels["summary"].items() if k != "e1_discrepancies"}, indent=2))
        return 0

    p.set_defaults(func=run)


def _register_leak(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("leak", help="E1 leak check: hide platform ids, search them in every remaining string field")
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
            "masked_indicator_values": sum(1 for i in hidden.indicators() if i.value and MASK in i.value),
            "masked_strings": hidden.to_json().count(MASK),
            "dataset_build": dataset_build(args.dataset),
            "hits": len(hits),
            "hit_rows": [h.as_dict() for h in hits],
        }
        write_json(args.out_dir / "leak" / "leak.json", out)
        print(json.dumps({k: v for k, v in out.items() if k != "hit_rows"}))
        for h in hits[:50]:
            print(f"HIT {h.hidden_kind} in {h.path} ({h.kind})")
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
        labels = build_labels(ds, load_spec(args.spec))
        engine = get_engine(args.engine)
        work = hide_platform_ids(ds)[0] if args.hide else ds
        clustered = engine.link_dataset(work)
        result = score_against_labels(clustered, labels)
        mode = "hidden" if args.hide else "visible"
        result.update({"engine": args.engine, "platform_ids": mode,
                       "indicators_used": len(work.indicators()),
                       "dataset_build": dataset_build(args.dataset)})
        write_json(args.out_dir / "baseline" / f"{args.engine}-{mode}.json", result)
        print(f"engine {args.engine}, platform ids {mode}, {len(work.indicators())} indicators used")
        for row in result["levels"]:
            pw, b3, mac = row["pairwise"], row["bcubed"], row["macro"]
            print(
                f"{row['relation']:>9} vs {row['level']:<16} pairwise P {pw['tp']}/{pw['predicted_pairs']} "
                f"R {pw['tp']}/{pw['true_pairs']}  B3 P {_f(b3['precision'])} R {_f(b3['recall'])} "
                f"(macro P {_f(mac['precision'])} R {_f(mac['recall'])}) "
                f"must-not-link violated {row['must_not_link']['violated']}/{row['must_not_link']['pairs']}"
            )
        return 0

    p.set_defaults(func=run)


def _openset_command(args: argparse.Namespace, *, modes: list[str], subdir: str, title: str) -> int:
    """Leave-one-out (E2/E3): run the open-set protocols for each mode and write JSON + markdown."""
    from ftf.evaluate.engine import get_engine
    from ftf.evaluate.labels import build_labels
    from ftf.evaluate.openset import FULL, run_openset, summarise, threshold_sweep, to_jsonable
    from ftf.evaluate.robustness import campaign_groups, openset_markdown

    ds = load_dataset(args)
    labels = build_labels(ds, load_spec(args.spec))
    groups = campaign_groups(labels)
    results = run_openset(get_engine(args.engine), ds, groups, modes=modes)
    payload = {
        "schema": "ftf-eval-openset@0.1",
        "dataset": ds.name,
        "dataset_build": dataset_build(args.dataset),
        "engine": args.engine,
        "relation": "campaign",
        "modes": modes,
        "groups": groups,
        "summary": summarise(results),
        "threshold_sweep": threshold_sweep(results) if FULL in modes else [],
        "items": to_jsonable(results),
    }
    out = args.out_dir / subdir / f"{args.engine}.json"
    write_json(out, payload)
    md = openset_markdown(title, payload)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _register_openset(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("openset", help="E2 leave-one-out open-set assignment, full inputs -> openset/<engine>.json")
    add_common(p)
    p.add_argument("--engine", default="linking", choices=["baseline", "linking"],
                   help="engine: baseline | linking (default: %(default)s)")

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.openset import FULL

        return _openset_command(args, modes=[FULL], subdir="openset",
                                title="E2 leave-one-out (one item, one whole group), full inputs")

    p.set_defaults(func=run)


def _register_degrade(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("degrade", help="E3 leave-one-out with the held-out item degraded to one modality -> degrade/<engine>.json")
    add_common(p)
    p.add_argument("--engine", default="linking", choices=["baseline", "linking"],
                   help="engine: baseline | linking (default: %(default)s)")

    def run(args: argparse.Namespace) -> int:
        from ftf.evaluate.openset import FULL
        from ftf.evaluate.perturb import MODES

        return _openset_command(args, modes=[FULL, *MODES], subdir="degrade",
                                title="E3 degraded inputs (full row kept as the reference)")

    p.set_defaults(func=run)


def _run_distractors(args: argparse.Namespace) -> int:
    """E4: every distractor checked against the engine; JSON + markdown with raw counts."""
    from ftf.evaluate.engine import get_engine
    from ftf.evaluate.labels import build_labels
    from ftf.evaluate.metrics import must_not_link, predicted_partition
    from ftf.evaluate.openset import run_distractors, to_jsonable
    from ftf.evaluate.perturb import CORPUS_TAGS_FILE, read_corpus_tags, with_incidents
    from ftf.evaluate.robustness import (
        distractor_markdown,
        distractor_plan,
        expectation_text,
        must_not_link_pairs,
    )

    ds = load_dataset(args)
    labels = build_labels(ds, load_spec(args.spec))
    corpus = Dataset.from_json(Path(args.corpus).read_bytes())
    tags = read_corpus_tags(Path(args.corpus).parent / CORPUS_TAGS_FILE)
    plan, notes = distractor_plan(ds, labels, corpus, tags, brand_limit=args.brand_limit)
    engine = get_engine(args.engine)
    results = run_distractors(engine, ds, plan)
    base = engine.link_dataset(with_incidents(ds))
    mnl = must_not_link(predicted_partition(base, "campaign"), must_not_link_pairs(labels))
    payload = {
        "schema": "ftf-eval-distractors@0.1",
        "dataset": ds.name,
        "dataset_build": dataset_build(args.dataset),
        "engine": args.engine,
        "selection": notes,
        "results": [
            {**to_jsonable([r])[0], "expected": expectation_text(d)}
            for d, r in zip(plan, results, strict=True)
        ],
        "must_not_link": {"pairs": mnl["pairs"], "violated": mnl["violated"], "violations": mnl["violations"]},
    }
    out = args.out_dir / "distractors" / f"{args.engine}.json"
    write_json(out, payload)
    md = distractor_markdown(payload)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _register_distractors(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("distractors", help="E4 distractors (must-not-link, kit copy, platform-only, collector, brand, celebrity) -> distractors/<engine>.json")
    add_common(p)
    p.add_argument("--engine", default="linking", choices=["baseline", "linking"],
                   help="engine: baseline | linking (default: %(default)s)")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                   help="background corpus batch-1 contract Dataset; its corpus_kind.jsonl tags sit beside it (default: %(default)s)")
    p.add_argument("--brand-limit", type=int, default=None,
                   help="check only the first N brand negatives (default: all tagged must_not_link)")
    p.set_defaults(func=_run_distractors)


# Subcommands; append one register function per experiment group.
SUBCOMMANDS: list[Callable[[argparse._SubParsersAction], None]] = [
    _register_labels,
    _register_leak,
    _register_baseline,
    _register_openset,
    _register_degrade,
    _register_distractors,
    register_timesplit,
    register_hidden,
    register_report,
]


def _guard(func: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            return func(args)
        except (EvalInputError, FileNotFoundError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"ftf eval: error: {exc}", file=sys.stderr)
            return 2

    return run


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("eval", help="evaluation harness (labels, leak check, metrics, baselines)")
    sub = parser.add_subparsers(dest="eval_command", metavar="<sub>")
    for reg in SUBCOMMANDS:
        reg(sub)
    for p in sub.choices.values():
        func = p.get_default("func")
        if func is not None:
            p.set_defaults(func=_guard(func))
    parser.set_defaults(func=lambda args: (parser.print_help(), 0)[1])
